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
A hook state that could not be verified (``arm_action.plan`` answers ``unknown``) is neither: it is
recorded as ``failed`` with a fingerprint that never matches, and reads ``stale`` every time.

Recording a stage against inputs other than its previous entry's marks every later entry
``invalidated`` on disk, so re-checking one stage cannot bring an older later result back to life;
those stages read ``stale`` until they are recorded again.
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
# ``required_checks`` adds the ones that depend on the loop (a configured adjudicator, its seats).
REQUIRED_CHECKS = {
    "identities": ("github:read", "github:reviewer", "github:fixer", "github:distinct",
                   "github:repo"),
    "loop": ("runtime:file", "runtime:source", "runtime:venv", "runtime:runtime", "runtime:rust",
             "bwrap:installed", "bwrap:userns", "sandbox:snapshot", "sandbox:secret-files",
             "sandbox:start", "sandbox:secrets", "sandbox:network", "sandbox:env",
             "sandbox:toolchain", "ledger"),
    "model": ("seat:reviewer", "seat:fixer", "model:completion"),
    "live_turn": ("broker:pr", "broker:reviewer", "broker:receipt", "turn:reviewer"),
    "armed": ("arm:hooks",),
}
# Any other check under these prefixes that FAILED also keeps its stage from being ``passed``
# (selftest's ``github:distinct-files``, ``github:adjudicator``, ``ledger:production``, ...).
FAILURE_SCOPE = {
    "identities": ("github:",),
    "loop": ("runtime:", "bwrap:", "sandbox:", "ledger"),
    "model": ("seat:", "model:"),
    "live_turn": ("broker:", "turn:"),
    "armed": ("arm:",),
}
# Checks selftest emits only when something stopped the stage early (SKIP: no usable runtime
# config; FAIL: unusable work root). Seen at all, they count and are never a ``pass``.
TROUBLE_CHECKS = {"loop": ("sandbox:containment",)}

# One vocabulary for the three sources: selftest's pass/fail/warn/skip, doctor's
# verified/absent/mismatch/unknown/skipped.
_OUTCOME = {"pass": "pass", "verified": "pass", "fail": "fail", "absent": "fail",
            "mismatch": "fail", "warn": "warn", "unknown": "warn", "skip": "skip",
            "skipped": "skip"}
_SUMMARY_MAX = 240


def path(loop: dict) -> Path:
    return config.state_dir(loop) / "onboarding.json"


_RANK = {"pass": 0, "skip": 1, "warn": 2, "fail": 3}


def required_checks(stage: str, loop: dict | None = None) -> tuple:
    """The checks ``stage`` needs, with the ones this loop adds (its adjudicator, its seats)."""
    names = list(REQUIRED_CHECKS[stage])
    if loop is not None:
        from . import seat_model
        if stage == "identities" and config.adjudicator_login(loop):
            names.append("github:adjudicator")
        if stage == "model":
            names += [f"seat:{seat}" for seat in seat_model.seats_for(loop)
                      if f"seat:{seat}" not in names]
    return tuple(names)


def normalize_checks(checks) -> dict:
    """``{name: (outcome, times seen)}`` from a mapping or an iterable of ``(name, outcome)``.

    A name seen more than once (selftest runs ``model:completion`` once per distinct seat model)
    keeps its worst outcome: one failing completion is a failure."""
    pairs = checks.items() if hasattr(checks, "items") else checks
    out: dict = {}
    for name, outcome in pairs:
        outcome = _OUTCOME.get(str(outcome).lower(), "warn")
        worst, count = out.get(name, (outcome, 0))
        out[name] = (outcome if _RANK[outcome] > _RANK[worst] else worst, count + 1)
    return out


def report_checks(results) -> list:
    """``(name, status)`` pairs from ``selftest.Report.results``, repeated names kept."""
    return [(result[1], result[2]) for result in results]


def stage_status(stage: str, requested: str, checks, *, loop: dict | None = None,
                 completions: int | None = None) -> str:
    """What ``requested`` may honestly be given the required checks' outcomes.

    ``checks`` is ``{name: outcome}`` or ``[(name, outcome), ...]`` (``normalize_checks``);
    ``None`` means the caller states the status itself. A FAIL in a required check, or in any
    other check of the stage's ``FAILURE_SCOPE``, is ``failed``; so is a WARN in a required one.
    A SKIP, or a required check that never ran, is ``skipped``. Neither is ever ``passed``.
    ``completions`` is the number of distinct seat models the loop resolves: fewer
    ``model:completion`` results than that is ``skipped``. A ``failed`` or ``skipped`` request
    stays as asked.
    """
    if requested != PASSED or checks is None:
        return requested
    seen = normalize_checks(checks)
    names = required_checks(stage, loop) + tuple(
        name for name in TROUBLE_CHECKS.get(stage, ()) if name in seen)
    outcomes = [seen[name][0] if name in seen else "skip" for name in names]
    outcomes += [outcome for name, (outcome, _) in seen.items()
                 if outcome == "fail" and name.startswith(FAILURE_SCOPE[stage])]
    if stage == "model" and completions and seen.get("model:completion", (0, 0))[1] < completions:
        outcomes.append("skip")
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


def _resolve_seats(loop: dict, runtime_file: Path, resolver) -> dict:
    """seat → its ``SeatInference`` (key included: never store it), or ``None`` if unresolved."""
    from . import seat_model
    try:
        settings = seat_model.load_runtime(runtime_file)
    except (OSError, ValueError):
        return {seat: None for seat in seat_model.seats_for(loop)}
    found = {}
    for seat in seat_model.seats_for(loop):
        try:
            found[seat] = seat_model.resolve_seat(loop, seat, settings, resolver=resolver)
        except seat_model.SeatModelError:
            found[seat] = None
    return found


def _seat_identities(seats: dict) -> dict:
    """seat → a hash of ``SeatInference.identity()``, or ``unresolved``."""
    return {seat: _digest(list(found.identity())) if found else "unresolved"
            for seat, found in seats.items()}


def _live_head(loop: dict, number: int) -> str:
    from . import gh
    pr, _ = gh.fetch(loop, f"/repos/{loop['repo']}/pulls/{number}", login=loop.get("read_token"))
    return str(((pr or {}).get("head") or {}).get("sha") or "") if isinstance(pr, dict) else ""


HOOK_STATES = ("armed", "paused")


class HookStateUnknown(Exception):
    """The hooks' state could not be verified; there is nothing to fingerprint."""


def _hook_state(loop: dict) -> str:
    from . import arm_action
    return str(arm_action.plan(loop).get("state") or "unknown")


def fingerprint(loop: dict, stage: str, *, pr: tuple | None = None,
                runtime_file: Path | None = None, resolver=None, hook_state=None,
                seats: dict | None = None) -> str:
    """The hash of ``stage``'s inputs as they are now (``pr`` is ``(number, head)``).

    ``seats`` is ``_resolve_seats``' answer when the caller already has it. Raises
    ``HookStateUnknown`` for ``armed`` when the hook state is not ``armed`` or ``paused``:
    ``arm_action.plan`` reports an unreadable, missing or mixed hook set as ``unknown``, and
    that must never match a later ``unknown``.
    """
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
        facts = _seat_identities(seats if seats is not None
                                 else _resolve_seats(loop, runtime_file, resolver))
    elif stage == "live_turn":
        if not str(pr[1] or "").strip():
            raise ValueError("no usable PR head: nothing to fingerprint")
        facts = {"pr": pr[0], "head": pr[1], "prompt": prompts.revision("reviewer")}
    elif stage == "armed":
        state = (hook_state or _hook_state)(loop)
        if state not in HOOK_STATES:
            raise HookStateUnknown(f"hook state is {state!r}, not one of {', '.join(HOOK_STATES)}")
        facts = {"hooks": state}
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


WITHHELD = "[withheld: {}]"
# The stages whose commands and summaries can carry a model key (a turn talks to the model).
MODEL_STAGES = ("model", "live_turn")
_MIN_REDACTABLE = 4      # ``selftest.Redactor.add`` ignores anything shorter


def _one_line(text: object, redact, withheld: str = "") -> str:
    if withheld:
        return WITHHELD.format(withheld)
    return " ".join(redact(text).split())[:_SUMMARY_MAX]


def _redactor(loop: dict, runtime_file: Path, seats: dict, stage: str, secrets=()):
    """``(redact, withheld)``: every configured credential's text, the model key files', each
    resolved seat's key (a Hermes profile's key has no token shape), the ``secrets`` the caller's
    check saw, and token shapes.

    ``withheld`` is why the command and summary must not be stored at all, or ``""``: the
    fail-closed cases. ``selftest.Redactor`` cannot redact a credential shorter than four
    characters, and a model-facing stage with a seat that did not resolve may carry a key this
    record never saw.
    """
    from . import selftest, seat_model
    redact = selftest.redactor_for(loop)
    texts = [inference.key for inference in seats.values() if inference is not None]
    texts += [secret for secret in secrets if secret]
    files = [Path(str(raw)).expanduser() for raw in config.credentials_map(loop).values() if raw]
    try:
        settings = seat_model.load_runtime(runtime_file)
    except (OSError, ValueError):
        settings = None
    if settings:
        for block in [seat_model.legacy_override(settings), *(settings.get("seats") or {}).values()]:
            if block:
                files.append(Path(block["key_file"]).expanduser())
    for file in files:
        try:
            texts.append(file.read_text())
        except Exception:
            pass
    withheld = ""
    for text in texts:
        redact.add(text)
        if isinstance(text, str) and 0 < len(text.strip()) < _MIN_REDACTABLE:
            withheld = "a credential is too short to redact"
    if not withheld and stage in MODEL_STAGES and any(found is None for found in seats.values()):
        withheld = "a seat's credentials could not be resolved, so its key cannot be redacted"
    return redact, withheld


def record(loop: dict, stage: str, status: str, command: str, summary: str, *,
           checks=None, pr: tuple | None = None, runtime_file: Path | None = None,
           resolver=None, hook_state=None, secrets=()) -> dict:
    """Store ``stage``'s result with the fingerprint of its inputs as they are now.

    ``secrets`` are credentials the caller's check saw (a key it resolved) that must not reach
    the file. If one cannot be redacted (shorter than four characters), or a model-facing stage
    (``MODEL_STAGES``) has a seat whose key was never resolved, the command and summary are
    withheld rather than stored.

    ``checks`` (``{name: outcome}`` or ``[(name, outcome), ...]`` of the stage's checks) keeps a
    SKIP, a WARN or a FAIL from being stored as ``passed`` (see ``stage_status``). ``live_turn``
    needs ``pr=(number, head)``. An ``armed`` result whose hook state cannot be verified is stored
    ``failed`` (never ``passed``) with a fingerprint that matches nothing. Recording a stage
    against other inputs than its previous entry marks the later entries ``invalidated``.
    Returns the stored entry.
    """
    if stage not in STAGES:
        raise ValueError(f"unknown onboarding stage {stage!r} (use {', '.join(STAGES)})")
    if status not in RECORDED:
        raise ValueError(f"status must be one of {', '.join(RECORDED)}, not {status!r}")
    if stage == "live_turn" and (not pr or len(pr) != 2 or not str(pr[1] or "").strip()):
        raise ValueError("the live_turn stage is recorded against a PR: pass pr=(number, head) "
                         "with the head SHA it was shown against")
    runtime_file = Path(runtime_file or config.host_path("runtime"))
    seats = _resolve_seats(loop, runtime_file, resolver)
    redact, withheld = _redactor(loop, runtime_file, seats, stage, secrets)
    try:
        prints = fingerprint(loop, stage, pr=pr, runtime_file=runtime_file, resolver=resolver,
                             hook_state=hook_state, seats=seats)
    except HookStateUnknown:
        prints, status = "", (FAILED if status == PASSED else status)
    distinct = len({found.identity() for found in seats.values() if found is not None})
    kept = required_checks(stage, loop)
    entry = {"status": stage_status(stage, status, checks, loop=loop, completions=distinct),
             "at": util.now_iso(),
             "command": _one_line(command, redact, withheld),
             "summary": _one_line(summary, redact, withheld),
             "checks": {name: outcome for name, (outcome, _) in normalize_checks(checks or {}).items()
                        if name in kept},
             "fingerprint": prints}
    if pr:
        entry["pr"] = int(pr[0])
    with _locked(loop):
        stages = _load(loop)
        before = stages.get(stage)
        stages[stage] = entry
        if not prints or not isinstance(before, dict) or before.get("fingerprint") != prints:
            # The later stages were shown against other inputs than these.
            for later in STAGES[STAGES.index(stage) + 1:]:
                if isinstance(stages.get(later), dict):
                    stages[later]["invalidated"] = True
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
            changed = entry.get("invalidated") is True or _differs(loop, stage, entry, runtime_file, resolver, pr_head, hook_state)
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
