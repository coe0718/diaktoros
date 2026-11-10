"""Rerun main's failed CI jobs once when the failure looks like a flake, before a red-main notice
(#1784, ``main_flake_rerun``, off unless set).

A flaky test on main sent a ``main_red`` notice blaming whatever merged last (#1778: one timeout in
the 3.11 shard, green in the 3.14 shard of the same run and on a rerun by hand). With the setting
on, ``main_check.sweep`` first asks ``decide`` about the failed required checks at main's head. A
check counts as a flake when either:

* every failing test in it also ran in a passed required job at the same head (a sibling shard or
  Python version) and passed there; or
* every failure in it is a timeout: the job hit its time limit, or each failure's traceback is a
  ``TimeoutExpired``/``TimeoutError``.

When every failed check is one, the host reruns the failed jobs of each workflow run once
(``POST /actions/runs/{id}/rerun-failed-jobs``) as a maintainer whose token the loop holds (never a
seat's login) and sends nothing yet. A rerun that comes back green sends one routine ``main_flaked``
notice naming the test(s), so flakes are counted rather than lost; a second red sends ``main_red``
as before, saying a rerun was tried. Never on a PR, once per main head, and anything it cannot read
is no flake: the notice goes out as today.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import time

from . import ci, ci_tests, config, gh

TESTS_MAX = 10
# How long a rerun may take to show at the head before a red read of the old jobs counts as red.
RERUN_WAIT_S = 2 * 3600
_BANNER = re.compile(r"^(?:\S+Z )?(?:FAIL|ERROR): test\w* \([\w.]+\)")
_END = re.compile(r"^(?:\S+Z )?(?:={20,}|Ran \d+ tests? in )")
_TIMEOUT = re.compile(r"\b(?:TimeoutExpired|TimeoutError|timed out)\b")


@dataclass
class Decision:
    """What ``main_check.sweep`` does with a red main head: ``notice`` (with ``note`` added to the
    ``main_red`` outcome), ``rerun`` (``record`` it, no notice) or ``wait`` (a rerun under way)."""
    action: str
    note: str = ""
    record: dict = field(default_factory=dict)


def rerun_login(loop: dict) -> str:
    """Who reruns: the first ``triage.maintainers`` login the loop holds a token for, never a seat's
    login; '' when there is none."""
    tokens = {str(name).lower() for name in (loop.get("tokens") or {})}
    seats = {str((seat or {}).get("login") or "").lower()
             for seat in (loop.get("seats") or {}).values() if isinstance(seat, dict)}
    seats.add(config.triage_login(loop).lower())
    for name in (loop.get("triage") or {}).get("maintainers") or []:
        login = str(name).strip().lower()
        if login and login in tokens and login not in seats:
            return login
    return ""


def timeouts(log: str) -> bool:
    """Every unittest failure in ``log`` is a timeout, and there is at least one."""
    blocks, current = [], None
    for line in log.splitlines():
        line = ci_tests._ANSI.sub("", line)
        if _BANNER.match(line):
            current = [line]
            blocks.append(current)
        elif current is not None:
            if _END.match(line):
                current = None
            else:
                current.append(line)
    return bool(blocks) and all(_TIMEOUT.search("\n".join(block)) for block in blocks)


def _failed_job(loop: dict, view: ci.CIState, name: str, head: str, runs: dict):
    """``(run id, conclusion, log)`` of a failed check that is an Actions job of this repository at
    ``head``; None when it is not one, or anything is unreadable."""
    job_id = view.ids.get(name)
    if type(job_id) is not int:
        return None
    job = ci_tests._job_at_head(loop, job_id, head, runs)
    if job is None:
        return None
    log = gh.read_text(loop, f"/repos/{loop['repo']}/actions/jobs/{job_id}/logs",
                       login=loop.get("read_token"), limit=ci_tests.LOG_LIMIT)
    return (job["run_id"], job.get("conclusion"), log) if log else None


def flake(loop: dict, view: ci.CIState, head: str) -> dict | None:
    """``{"runs", "tests", "why"}`` when every failed required check at ``head`` looks like a
    flake, else None."""
    required = config.required_checks(loop)
    if not required or not view.failed:
        return None
    runs, tests, why, passed, checked = set(), set(), set(), None, {}
    for name in view.failed:
        found = _failed_job(loop, view, name, head, checked)
        if found is None:
            return None
        run_id, conclusion, log = found
        failing = {test for test, result in ci_tests.parse(log).items() if result in ("FAIL", "ERROR")}
        if conclusion == "timed_out" or timeouts(log):
            why.add("timed out")
            tests |= failing or {f"{name} (timed out)"}
        else:
            passed = ci_tests.read(loop, view, required, head) if passed is None else passed
            if not failing or any(passed.get(test) != "ok" for test in failing):
                return None
            why.add("passed in a sibling job")
            tests |= failing
        runs.add(run_id)
    return {"runs": sorted(runs), "tests": sorted(tests)[:TESTS_MAX], "why": " / ".join(sorted(why))}


def decide(loop: dict, memory: dict, view: ci.CIState, head: str, now: float | None = None) -> Decision:
    """What to do about a red main ``head`` before its ``main_red`` notice (see the module)."""
    if not config.main_flake_rerun(loop):
        return Decision("notice")
    now = time.time() if now is None else now
    tried = memory.get("rerun")
    if isinstance(tried, dict) and tried.get("head") == head:
        failed_ids = {view.ids.get(name) for name in view.failed}
        if failed_ids <= set(tried.get("ids") or ()) and now - (tried.get("at") or 0) < RERUN_WAIT_S:
            return Decision("wait")                   # still the jobs that were rerun
        return Decision("notice", " A rerun of the failed jobs was tried, and it failed again.")
    who = rerun_login(loop)
    if not who:
        return Decision("notice", " (main_flake_rerun is on, but the loop holds no maintainer "
                                  "token that is not a seat's: no rerun was tried.)")
    found = flake(loop, view, head)
    if found is None:
        return Decision("notice")
    for run_id in found["runs"]:
        gh.api(loop, f"/repos/{loop['repo']}/actions/runs/{run_id}/rerun-failed-jobs",
               method="POST", login=who)
        if gh.last_error():
            return Decision("notice", f" It looked like a flake ({found['why']}), but the rerun "
                                      f"of run {run_id} failed: {gh.last_error()[:120]}.")
    ids = sorted(i for i in (view.ids.get(name) for name in view.failed) if type(i) is int)
    return Decision("rerun", record={**found, "head": head, "ids": ids, "at": now, "by": who})


def flaked_outcome(tried: dict) -> str:
    """The ``main_flaked`` notice's words for a rerun that came back green."""
    tests = ", ".join(tried.get("tests") or []) or "the failed jobs"
    return (f"main flaked: {tests} ({tried.get('why') or 'flake'}); its failed jobs were rerun "
            f"once as {tried.get('by') or 'a maintainer'} and passed. No PR is blamed.")
