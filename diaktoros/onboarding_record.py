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

import os
from pathlib import Path

from . import config, gh, onboarding

PASS, FAIL, SKIP = "pass", "fail", "skip"      # selftest's status words
TURN_CHECK = "turn:reviewer"                   # reported only once a turn has started
_STAGE_OF = {"identities": ("github:",), "loop": onboarding.FAILURE_SCOPE["loop"],
             "model": onboarding.FAILURE_SCOPE["model"],
             "live_turn": onboarding.FAILURE_SCOPE["live_turn"]}


def attempt(say, loop: dict, stage: str, *args, **kwargs) -> bool:
    """``onboarding.record`` that cannot fail the caller: on any error say so and return False."""
    return _attempt_entry(say, loop, stage, *args, **kwargs) is not None


def _attempt_entry(say, loop: dict, stage: str, *args, **kwargs) -> dict | None:
    """``attempt`` that returns the stored entry, or ``None`` when it could not be recorded."""
    try:
        return onboarding.record(loop, stage, *args, **kwargs)
    except Exception as exc:       # noqa: BLE001 - the check's own result must stand
        say(f"  (onboarding: the {stage} stage could not be recorded: "
            f"{type(exc).__name__}: {exc}; the result above stands)")
        return None


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


LOOP_FILE = "loop file"      # ``inputs_before``'s key for whether the loop has a config file


def _loop_file(loop: dict) -> str:
    """``"on disk"`` when the loop's config file exists, ``"in memory"`` when it has none (a
    loop a caller built, as tests do), ``""`` when that cannot be told."""
    try:
        exists = os.path.lexists(config.config_dir() / f"{loop['id']}.json")
    except Exception:              # noqa: BLE001 - unknown never matches
        return ""
    return "on disk" if exists else "in memory"


def inputs_before(loop: dict, *, runtime_file: Path, resolver=None) -> dict:
    """``{stage: fingerprint}`` for the stages selftest checks, taken before its checks run
    (#1714), and whether the loop has a config file (``LOOP_FILE``). ``record_selftest`` hands
    each stage's to ``onboarding.record`` as ``inputs``, so a token, key, setting or runtime
    file that changes during the run is never credited as checked. ``""`` (cannot be
    computed) never matches, so that stage is not recorded as current."""
    prints = {stage: onboarding.fingerprint_now(loop, stage, runtime_file=runtime_file,
                                                resolver=resolver)
              for stage in ("identities", "loop", "model")}
    return {**prints, LOOP_FILE: _loop_file(loop)}


def changed_during_run(loop: dict, inputs: dict, *, runtime_file: Path, resolver=None) -> list:
    """The stages in ``inputs`` (``inputs_before``'s answer) whose inputs no longer hash to it:
    a token, key, setting or runtime file that changed while the checks ran (#1714 review F3).
    The checks saw the old inputs, so the run must not end as a pass on the new ones.

    The loop's settings are read again from its file (#2525): ``loop`` is the copy loaded
    before the checks, so a ``set`` or a hand edit made during the run is only seen on disk.
    A loop file that is there but cannot be loaded now changes every stage; a loop that has
    no file (one built in memory) is compared as it is, and a file that came or went is
    ``LOOP_FILE`` changing."""
    current = loop
    if _loop_file(loop) != "in memory":
        try:
            current = config.load_id(loop["id"])
        except Exception:          # noqa: BLE001 - a loop that cannot be read verifies nothing
            return list(inputs)
    now = inputs_before(current, runtime_file=runtime_file, resolver=resolver)
    return [stage for stage, before in inputs.items() if now.get(stage) != before]


def record_selftest(loop: dict, report, *, say, pr: int | None, live_pr: dict | None,
                    model: bool, live_turn: bool, seats: dict | None,
                    runtime_file: Path, resolver=None, inputs: dict | None = None) -> list:
    """Record the stages this selftest run covers; returns the stages written, in order.

    ``inputs`` is ``inputs_before``'s answer from the start of the run; ``None`` (a caller that
    took none) records against the inputs as they are now. A stage stored against other inputs
    than those (they changed after selftest's last ``inputs:changed`` check, so
    ``onboarding.record`` stored it with no fingerprint) adds an ``inputs:changed`` FAIL to
    ``report`` (#2526): the run's result must match what was recorded."""
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
    downgraded = []
    for stage, status, extra in plan:
        if inputs is not None and stage in inputs:
            extra = {**extra, "inputs": inputs[stage]}
        entry = _attempt_entry(say, loop, stage, status, command, _summary(results, stage),
                               checks=checks, **common, **extra)
        if entry is not None:
            written.append(stage)
            if "inputs" in extra and entry.get("fingerprint") != extra["inputs"]:
                downgraded.append(stage)
    _report_downgraded(report, loop, downgraded)
    return written


def _report_downgraded(report, loop: dict, stages: list) -> None:
    """An ``inputs:changed`` FAIL naming ``stages``, unless the run already failed one."""
    if not stages or any(r[1] == "inputs:changed" and r[2] == FAIL for r in report.results):
        return
    report.add("inputs", "inputs:changed", FAIL,
               f"changed before the result was recorded: {', '.join(stages)} (recorded as "
               "failed: what passed above was checked against the old inputs)",
               f"re-run `hermes dk selftest --loop {loop.get('id') or '<id>'}` once the "
               "token, key or setting is no longer changing")


def record_arm(loop: dict, *, say, pause: bool, ok: bool, login: str | None = None) -> bool:
    """Record ``armed`` after ``arm`` for one loop; True when written.

    Only for a confirmed arm: not ``--pause``, ``perform`` ok, and a fresh plan whose state is
    ``armed`` (which is also the ``hook_state`` stored). The plan reads the hooks as ``login``
    (``arm --admin-token``), else as the reader. Nothing is recorded otherwise; a confirmed arm
    whose fresh plan is not ``armed`` says why.
    """
    if pause or not ok:
        return False
    try:
        from . import arm_action
        fresh = arm_action.plan(loop, login or None)
        state = str(fresh.get("state") or "unknown")
    except Exception as exc:       # noqa: BLE001
        say(f"  (onboarding: the armed stage could not be recorded: {type(exc).__name__}: {exc}; "
            "the result above stands)")
        return False
    if state != "armed":
        say(f"  (onboarding: the armed stage was not recorded: a fresh read of the hooks says "
            f"{state!r}, not 'armed' ({fresh.get('reason') or 'no reason given'}); "
            "the result above stands)")
        return False
    return attempt(say, loop, "armed", onboarding.PASSED, f"hermes dk arm --loop {loop['id']}",
                   "GitHub confirmed every loop hook active (read back)",
                   checks=[("arm:hooks", PASS)], hook_state=lambda _loop: state)
