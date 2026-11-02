"""What ``selftest`` and ``arm`` write to the onboarding record (#1617, part 3b of #677).

No check logic lives here and none is copied: each caller hands over the result it already
computed, and ``onboarding.record`` turns it into a stage entry. Which command owns which stage:

* ``selftest`` records ``identities``, ``loop``, ``model`` (``skipped`` for ``--no-model``, never
  ``passed``) and, only when it ran a live turn on a PR, ``live_turn``.
* ``arm`` records ``armed``, only for a confirmed arm (not ``--pause``, ``perform`` ok, and a fresh
  ``arm_action.plan`` that reports ``armed``).
* ``doctor`` records nothing: it only produces the ``runtime:*`` checks.

A record that cannot be written never changes a command's result or exit code: every write goes
through ``attempt``, which prints that the stage could not be recorded and carries on.
"""
from __future__ import annotations

from pathlib import Path

from . import gh, onboarding

PASS, FAIL, SKIP = "pass", "fail", "skip"      # selftest's status words
TURN_CHECK = "turn:reviewer"                   # reported only once a turn has started
_STAGE_OF = {"identities": ("github:",), "loop": onboarding.FAILURE_SCOPE["loop"],
             "model": onboarding.FAILURE_SCOPE["model"]}


def attempt(say, loop: dict, stage: str, *args, **kwargs) -> bool:
    """``onboarding.record`` that cannot fail the caller: on any error say so and return False."""
    try:
        onboarding.record(loop, stage, *args, **kwargs)
        return True
    except Exception as exc:       # noqa: BLE001 - the check's own result must stand
        say(f"  (onboarding: the {stage} stage could not be recorded: "
            f"{type(exc).__name__}: {exc}; the result above stands)")
        return False


def _summary(results, stage: str) -> str:
    """One line from the results already in the report: how many checks, and which failed."""
    scope = _STAGE_OF.get(stage)
    rows = [r for r in results if scope is None or r[1].startswith(scope)]
    bad = [r[1] for r in rows if r[2] in (FAIL, "warn")]
    skipped = [r[1] for r in rows if r[2] == SKIP]
    text = f"{len(rows)} checks"
    if bad:
        text += f"; not passing: {', '.join(bad)}"
    if skipped:
        text += f"; skipped: {', '.join(skipped)}"
    return text


def _secrets(loop: dict, seats: dict) -> list:
    """The credentials the checks saw: each resolved seat's key and each PAT file's text."""
    found = [inference.key for inference in (seats or {}).values() if inference is not None]
    for login in (loop.get("tokens") or {}):
        try:
            path = gh.token_path(loop, login)
            found.append(path.read_text().strip() if path else "")
        except Exception:          # noqa: BLE001 - record() withholds when a file is unreadable
            continue
    return [secret for secret in found if secret]


def selftest_command(loop: dict, *, pr, model: bool, live_turn: bool) -> str:
    flags = ("" if model else " --no-model") + (f" --pr {pr}" if pr is not None else "")
    return f"hermes dk selftest --loop {loop['id']}{flags}" + (" --live-turn" if live_turn else "")


def record_selftest(loop: dict, report, *, say, pr: int | None, live_pr: dict | None,
                    model: bool, live_turn: bool, seats: dict | None,
                    runtime_file: Path, resolver=None) -> list:
    """Record the stages this selftest run covers; returns the stages written, in order."""
    results = report.results
    checks = onboarding.report_checks(results)
    command = selftest_command(loop, pr=pr, model=model, live_turn=live_turn)
    common = {"runtime_file": runtime_file, "resolver": resolver,
              "secrets": _secrets(loop, seats)}
    written = []
    plan = [("identities", onboarding.PASSED, {}),
            ("loop", onboarding.PASSED, {}),
            ("model", onboarding.PASSED if model else onboarding.SKIPPED, {})]
    # A live turn is recorded only when one ran: the turn's own verdict check, not a SKIP, on a PR.
    # ``turn:prompt`` is no evidence: run_live_turn reports it FAIL and returns before any turn.
    ran = any(r[1] == TURN_CHECK and r[2] != SKIP for r in results)
    if live_turn and live_pr is not None and ran:
        head = str((live_pr.get("head") or {}).get("sha") or "")
        plan.append(("live_turn", onboarding.PASSED, {"pr": (live_pr.get("number"), head)}))
    for stage, status, extra in plan:
        if attempt(say, loop, stage, status, command, _summary(results, stage),
                   checks=checks, **common, **extra):
            written.append(stage)
    return written


def record_arm(loop: dict, *, say, pause: bool, ok: bool) -> bool:
    """Record ``armed`` after ``arm`` for one loop; True when written.

    Only for a confirmed arm: not ``--pause``, ``perform`` ok, and a fresh plan whose state is
    ``armed`` (which is also the ``hook_state`` stored). Nothing is recorded otherwise.
    """
    if pause or not ok:
        return False
    try:
        from . import arm_action
        state = str(arm_action.plan(loop).get("state") or "unknown")
    except Exception as exc:       # noqa: BLE001
        say(f"  (onboarding: the armed stage could not be recorded: {type(exc).__name__}: {exc}; "
            "the result above stands)")
        return False
    if state != "armed":
        return False
    return attempt(say, loop, "armed", onboarding.PASSED, f"hermes dk arm --loop {loop['id']}",
                   "GitHub confirmed every loop hook active (read back)",
                   checks=[("arm:hooks", PASS)], hook_state=lambda _loop: state)
