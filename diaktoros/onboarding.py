"""The onboarding record (#1598, part 1 of #677): which setup stage has been shown to work, and
whether what it was shown against has since changed.

Only the record lives here. The checks stay where they are (``selftest``, ``doctor``,
``arm_action``); this module stores what they found, one entry per stage, in
``<state_dir>/onboarding.json`` (0600, ``util.atomic_write``). Each entry holds the status
(``passed``/``failed``/``skipped``), the time, the command that produced it, a redacted one-line
summary, the required checks' outcomes, and a *fingerprint* of the stage's inputs.

A fingerprint is a SHA-256 over facts, never a secret: logins and file paths (not file contents),
a hash of the runtime file, the seats' ``SeatInference.identity()`` (hashed again, because that
tuple carries a digest of the key), a PR number and head SHA, the observed hook state.

``read`` gives each stage's status. A stage whose inputs no longer hash to its recorded
fingerprint reads ``stale``, and so does every recorded stage after it: a later stage was shown
against an earlier one's inputs. A stage never recorded reads ``not_attempted``.

What cannot be known offline: ``live_turn`` compares the PR's current head, and ``armed`` the
repo's current hooks, so ``read`` makes GitHub reads for those two stages (only when they were
recorded). A read that fails counts as a change: the stage reads ``stale``, never ``passed``.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
from pathlib import Path

from . import config, hostdirs, prompts, util

STAGES = ("identities", "loop", "model", "live_turn", "armed")
PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"
RECORDED = (PASSED, FAILED, SKIPPED)
NOT_ATTEMPTED, STALE = "not_attempted", "stale"

# The checks each stage needs, by the name selftest (``report.add``), doctor or arm_action gives
# them. A stage is ``passed`` only when every one of these was seen and passed.
REQUIRED_CHECKS = {
    "identities": ("github:read", "github:reviewer", "github:fixer", "github:distinct",
                   "github:repo"),
    "loop": ("runtime:file", "runtime:source", "runtime:venv", "runtime:runtime", "runtime:rust",
             "bwrap:installed", "bwrap:userns", "sandbox:containment", "ledger"),
    "model": ("seat:reviewer", "seat:fixer", "model:completion"),
    "live_turn": ("broker:pr", "broker:reviewer", "broker:receipt", "turn:reviewer"),
    "armed": ("arm:hooks",),
}

# One vocabulary for the three sources: selftest's pass/fail/warn/skip, doctor's
# verified/absent/mismatch/unknown/skipped.
_OUTCOME = {"pass": "pass", "verified": "pass", "fail": "fail", "absent": "fail",
            "mismatch": "fail", "warn": "warn", "unknown": "warn", "skip": "skip",
            "skipped": "skip"}
_SUMMARY_MAX = 240


def path(loop: dict) -> Path:
    return config.state_dir(loop) / "onboarding.json"


def stage_status(stage: str, requested: str, checks: dict | None) -> str:
    """What ``requested`` may honestly be given the required checks' outcomes.

    ``checks`` is ``{check name: outcome}``; ``None`` means the caller states the status itself.
    A FAIL in a required check is ``failed``; a SKIP, a WARN, or a required check that never ran
    is never ``passed`` either (``skipped`` for a SKIP or a missing check, ``failed`` for a WARN).
    A ``failed`` or ``skipped`` request stays as asked.
    """
    if requested != PASSED or checks is None:
        return requested
    seen = {name: _OUTCOME.get(str(outcome).lower(), "warn") for name, outcome in checks.items()}
    outcomes = [seen.get(name, "skip") for name in REQUIRED_CHECKS[stage]]
    if "fail" in outcomes or "warn" in outcomes:
        return FAILED
    return SKIPPED if "skip" in outcomes else PASSED


# -- fingerprints --------------------------------------------------------------------------

def _digest(value) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def _runtime_hash(runtime_file: Path) -> str:
    try:
        return hashlib.sha256(runtime_file.read_bytes()).hexdigest()
    except OSError:
        return "absent"


def _seat_identities(loop: dict, runtime_file: Path, resolver) -> dict:
    """seat → a hash of ``SeatInference.identity()``, or ``unresolved``."""
    from . import seat_model
    try:
        settings = seat_model.load_runtime(runtime_file)
    except (OSError, ValueError):
        return {seat: "unresolved" for seat in seat_model.seats_for(loop)}
    found = {}
    for seat in seat_model.seats_for(loop):
        try:
            found[seat] = _digest(list(seat_model.resolve_seat(
                loop, seat, settings, resolver=resolver).identity()))
        except seat_model.SeatModelError:
            found[seat] = "unresolved"
    return found


def _live_head(loop: dict, number: int) -> str:
    from . import gh
    pr, _ = gh.fetch(loop, f"/repos/{loop['repo']}/pulls/{number}", login=loop.get("read_token"))
    return str(((pr or {}).get("head") or {}).get("sha") or "") if isinstance(pr, dict) else ""


def _hook_state(loop: dict) -> str:
    from . import arm_action
    return str(arm_action.plan(loop).get("state") or "unknown")


def fingerprint(loop: dict, stage: str, *, pr: tuple | None = None,
                runtime_file: Path | None = None, resolver=None, hook_state=None) -> str:
    """The hash of ``stage``'s inputs as they are now (``pr`` is ``(number, head)``)."""
    runtime_file = Path(runtime_file or config.host_path("runtime"))
    if stage == "identities":
        # Logins and file paths (an App's id and key_file path), never the files' contents.
        facts = {"repo": loop.get("repo"), "tokens": loop.get("tokens") or {},
                 "apps": loop.get("apps") or {}}
    elif stage == "loop":
        from . import seat_model
        facts = {"runtime": _runtime_hash(runtime_file),
                 "profiles": {seat: str(config.profile_dir(config.seat_profile(loop, seat)))
                              for seat in seat_model.seats_for(loop)}}
    elif stage == "model":
        facts = _seat_identities(loop, runtime_file, resolver)
    elif stage == "live_turn":
        facts = {"pr": pr[0], "head": pr[1], "prompt": prompts.revision("reviewer")}
    elif stage == "armed":
        facts = {"hooks": (hook_state or _hook_state)(loop)}
    else:
        raise ValueError(f"unknown onboarding stage {stage!r}")
    return _digest([stage, facts])


# -- the file ------------------------------------------------------------------------------

@contextlib.contextmanager
def _locked(loop: dict):
    root = hostdirs.ensure(config.state_dir(loop), 0o700)
    with (root / ".onboarding.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _load(loop: dict) -> dict:
    """The stored entries; an unreadable or malformed file reads as nothing recorded."""
    try:
        data = json.loads(path(loop).read_text())
    except (OSError, ValueError):
        return {}
    stages = data.get("stages") if isinstance(data, dict) else None
    return stages if isinstance(stages, dict) else {}


def _one_line(text: object, redact) -> str:
    return " ".join(redact(text).split())[:_SUMMARY_MAX]


def _redactor(loop: dict, runtime_file: Path):
    """Every configured credential's text, the model key files', and token shapes."""
    from . import selftest, seat_model
    redact = selftest.redactor_for(loop)
    try:
        settings = seat_model.load_runtime(runtime_file)
    except (OSError, ValueError):
        return redact
    for block in [seat_model.legacy_override(settings), *(settings.get("seats") or {}).values()]:
        if block:
            redact.add_file(Path(block["key_file"]).expanduser())
    return redact


def record(loop: dict, stage: str, status: str, command: str, summary: str, *,
           checks: dict | None = None, pr: tuple | None = None,
           runtime_file: Path | None = None, resolver=None, hook_state=None) -> dict:
    """Store ``stage``'s result with the fingerprint of its inputs as they are now.

    ``checks`` ({name: outcome} of the stage's required checks) keeps a SKIP or WARN from being
    stored as ``passed`` (see ``stage_status``). ``live_turn`` needs ``pr=(number, head)``.
    Returns the stored entry.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown onboarding stage {stage!r} (use {', '.join(STAGES)})")
    if status not in RECORDED:
        raise ValueError(f"status must be one of {', '.join(RECORDED)}, not {status!r}")
    if stage == "live_turn" and (not pr or len(pr) != 2):
        raise ValueError("the live_turn stage is recorded against a PR: pass pr=(number, head)")
    runtime_file = Path(runtime_file or config.host_path("runtime"))
    redact = _redactor(loop, runtime_file)
    entry = {"status": stage_status(stage, status, checks),
             "at": util.now_iso(),
             "command": _one_line(command, redact),
             "summary": _one_line(summary, redact),
             "checks": {name: _OUTCOME.get(str(outcome).lower(), "warn")
                        for name, outcome in (checks or {}).items() if name in REQUIRED_CHECKS[stage]},
             "fingerprint": fingerprint(loop, stage, pr=pr, runtime_file=runtime_file,
                                        resolver=resolver, hook_state=hook_state)}
    if pr:
        entry["pr"] = int(pr[0])
    with _locked(loop):
        stages = _load(loop)
        stages[stage] = entry
        util.atomic_write(path(loop), json.dumps({"version": 1, "stages": stages},
                                                 indent=2, sort_keys=True) + "\n", mode=0o600)
    return entry


def read(loop: dict, *, runtime_file: Path | None = None, resolver=None, pr_head=None,
         hook_state=None) -> dict:
    """Each stage's status: what was recorded, ``not_attempted``, or ``stale``.

    A recorded stage whose fingerprint no longer matches is ``stale``, and so is every recorded
    stage after it. ``pr_head(loop, number)`` and ``hook_state(loop)`` read GitHub (injectable);
    when they cannot be read the stage is ``stale``.
    """
    runtime_file = Path(runtime_file or config.host_path("runtime"))
    stored = _load(loop)
    out: dict = {}
    changed = False
    for stage in STAGES:
        entry = stored.get(stage)
        if (not isinstance(entry, dict) or entry.get("status") not in RECORDED
                or not isinstance(entry.get("fingerprint"), str)):
            out[stage] = NOT_ATTEMPTED
            continue
        if not changed:
            changed = _differs(loop, stage, entry, runtime_file, resolver, pr_head, hook_state)
        out[stage] = STALE if changed else entry["status"]
    return out


def _differs(loop, stage, entry, runtime_file, resolver, pr_head, hook_state) -> bool:
    pr = None
    if stage == "live_turn":
        number = entry.get("pr")
        if type(number) is not int:
            return True
        try:
            pr = (number, (pr_head or _live_head)(loop, number))
        except Exception:
            return True
    try:
        return fingerprint(loop, stage, pr=pr, runtime_file=runtime_file, resolver=resolver,
                           hook_state=hook_state) != entry["fingerprint"]
    except Exception:
        return True
