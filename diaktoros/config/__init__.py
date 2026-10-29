"""Loop configuration: one JSON file per loop under ``~/.hermes/diaktoros.d/`` (``review-loops.d/``
before the rename, until ``migrate`` moves it).

Everything a run must know about a repository is data here — the repo, its seats, its
budget, its credential files, the roots it is allowed to clean. Nothing is a constant in
the code, which is what lets one install serve several repositories with different
settings, and what keeps a stranger's repo names and handles out of the source.

File shape (all keys except ``repo`` have defaults)::

    {
      "id": "name",
      "repo": "owner/name",
      "base": "main",
      "cap": 3,
      "fixers": ["dev-account"],
      "reviewers": ["rev-bot"],
      "reviewer_seat": "rev-bot",
      "seats": {
        "reviewer": {"profile": "critic", "route": "name-review",
                     "login": "rev-bot", "agent": "Critic"},
        "fixer":    {"profile": "coder", "route": "name-fix",
                     "login": "dev-account", "agent": "Coder"}
      },
      "adjudicator": {"route": "name-breach", "profile": "arbiter"},
      "observer": {"route": "name-observe", "profile": "arbiter", "deliver": "telegram",
                   "events": ["opened", "handoff", "verdict", "approved", "escalation",
                              "ruling", "stall", "closed"],
                   "digest_min": 0, "mute": false},
      "read_token": "reader-bot",
      "tokens": {"reader-bot": "~/.hermes/keys/reader-bot-pat",
                 "rev-bot": "~/.hermes/keys/rev-bot-pat",
                 "dev-account": "~/.hermes/keys/dev-account-pat"},
      "state_dir": "~/.hermes/state/diaktoros/name",
      "clone": "~/projects/name",
      "roots": ["~/reviews", "~/.hermes/cache/scratch"],
      "grace_min": 35, "marker_grace_min": 60, "cooldown_h": 6,
      "ttl_min": 45, "inflight_ttl_min": 10, "turn_budget_s": 900
    }

``config`` is deliberately strict: a loop that cannot be resolved to a repository,
a base branch and two seats is a configuration error, not a run that guesses.

The one key that is *not* strict is ``observer`` — the optional read-only feed (see
``diaktoros.observer``). A seat that cannot be resolved is a loop that cannot turn; an
observer that cannot be resolved is a loop that turns without telling anyone, so a broken feed
is dropped with its reason kept under ``misconfigured`` for ``status`` to report.
"""

from __future__ import annotations

import contextlib

import ipaddress
import json
import math
import os
import pathlib
import re
from urllib.parse import urlsplit
from urllib.request import Request

from .. import envnames
from . import flags

# Seat identity: *who* a seat is. A profile decides the model, the budget and the credentials the
# run happens with; a login decides the GitHub attribution. Both are per loop — two repositories can
# legitimately be served by different profiles — so the plugin settings only ever supply defaults a
# new loop starts from, and `apply --loop` pushes them onto one loop at a time.
PROFILE_SETTINGS: dict = {"reviewer": "reviewer_profile", "fixer": "fixer_profile",
                          "adjudicator": "adjudicator_profile"}
LOGIN_SETTINGS: dict = {"reviewer": "reviewer_login", "fixer": "fixer_login"}
# Where each role's PAT lives. Paths, never values: see ``token_file_problem``.
TOKEN_FILE_SETTINGS: dict = {"reviewer": "reviewer_token_file", "fixer": "fixer_token_file",
                             "adjudicator": "adjudicator_token_file"}
# Every role that can own a webhook route. The adjudicator is here but not in ``SEAT_KEYS``: it has
# a route and a profile, and no login or allowlist of its own.
ROUTE_ROLES = ("reviewer", "fixer", "adjudicator")
# Every role a route can wake, issue triage (#213) included. Triage is opt-in per loop, so the
# seat listings (``ROUTE_ROLES``) leave it out; route writing and ownership checks use this.
ROUTED_ROLES = (*ROUTE_ROLES, "triage")
# The GitHub event each hooked role's repo hook delivers (#213). The adjudicator has a route but no
# repo hook (the loop wakes it itself), so it is not here.
HOOK_EVENT = {"reviewer": "pull_request", "fixer": "pull_request_review", "triage": "issues"}
# Gate scripts an older release of *this* plugin installed for a role. Before the dedicated
# adjudicator gate (PR #21) the breach route ran gate_reviewer.py. Such a route is still ours —
# its prompt proves it — so ``apply`` rebinds it in place instead of refusing it as foreign, and
# ``doctor`` points there rather than at ``init``, which refuses an existing loop.
LEGACY_GATE_SCRIPTS: dict = {"adjudicator": frozenset({"gate_reviewer.py"})}

# A Hermes profile name is a directory name under ``profiles/``. Refusing separators and dots-only
# names here is what keeps a typo from resolving to somewhere outside the profiles root.
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _form_int(value):
    """A form number: whole-number text becomes an int; anything else is left for the checker."""
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    return value


def profiles_root() -> pathlib.Path:
    return home() / "profiles"


def profile_dir(name: str) -> pathlib.Path:
    """Where a Hermes profile lives, whether or not it exists.

    The launch profile *is* the Hermes home; every other profile is a directory under
    ``profiles/``. Nothing is created here on purpose — a validation that mkdir'd would turn a
    typo into what looks like a working install.
    """
    name = str(name or "").strip()
    return home() if name in ("", "default") else profiles_root() / name


def profile_exists(name: str) -> bool:
    name = str(name or "").strip()
    if not name or not _PROFILE_NAME.fullmatch(name):
        return False
    if name == "default":
        return True
    profile = profile_dir(name)
    marker = profile / "config.yaml"
    # A symlink can give two differently named seats the same credentials and budget.
    # Do not accept an alias as a named profile, even if its target has a config.
    return (not profile.is_symlink() and profile.is_dir()
            and marker.is_file() and marker.stat().st_size > 0)


def seat_mapping(settings: dict | None) -> dict:
    """The seat identities the settings form names — only the keys the operator actually filled in.

    A blank profile/login is *not set here*, never "forget what this loop uses": that is what lets
    one per-profile form hold defaults for a new loop without quietly rewriting seats a loop
    already answered for itself.
    """
    # Imported here, not at module level: a module-level import would also bind
    # config.settings_defaults, a second home for a moved name (test_config_modules refuses it).
    from .settings_form import settings_defaults
    d = settings_defaults(settings)
    mapping: dict = {}
    for seat, key in PROFILE_SETTINGS.items():
        entry: dict = {}
        if str(d.get(key) or "").strip():
            entry["profile"] = str(d[key]).strip()
        login_key = LOGIN_SETTINGS.get(seat) or ("adjudicator_login" if seat == "adjudicator"
                                                 else "")
        if login_key and str(d.get(login_key) or "").strip():
            entry["login"] = str(d[login_key]).strip()
        token_key = TOKEN_FILE_SETTINGS.get(seat)
        if token_key and str(d.get(token_key) or "").strip():
            entry["token_file"] = str(d[token_key]).strip()
        if seat in TOKEN_FILE_SETTINGS:
            for field in ("app_id", "app_key_file"):
                value = str(d.get(f"{seat}_{field}") or "").strip()
                if value:
                    entry[field] = value
        if entry:
            mapping[seat] = entry
    return mapping


def seat_profile(loop: dict, role: str) -> str:
    """What a seat (or the adjudicator, or the observer feed) runs as, from the config it is
    actually driving with.

    This is the one answer to "which profile does this role's route serve": ``init`` and ``set``
    write the route with it, and ``apply``/``doctor``/the gate shims check the route against it.
    The observer is not a seat, but its route still carries a profile; a loop with no observer
    route has none (``""``).
    """
    if role == "adjudicator":
        return str((loop.get("adjudicator") or {}).get("profile") or "default")
    if role == "app":
        # #1239: an App's route is shared by every loop it delivers for, so it never follows one
        # loop's seat; its gates end [SILENT], so no agent runs as this profile.
        return "default" if app_ingress(loop) else ""
    if role == "triage":
        return str((loop.get("triage") or {}).get("profile") or "")
    if role == "shadow":
        return shadow_profile(loop)
    if role == "issue_fixer":
        role = "fixer"           # an issue fix runs as the fixer seat (#214)
    if role == "observer_urgent":
        observer = loop.get("observer") or {}
        if not str(observer.get("urgent_route") or "").strip():
            return ""
        return (str(observer.get("urgent_profile") or observer.get("profile") or "default").strip()
                or "default")
    if role == "observer":
        observer = loop.get("observer") or {}
        if not str(observer.get("route") or "").strip():
            return ""
        return str(observer.get("profile") or "default").strip() or "default"
    return str(((loop.get("seats") or {}).get(role) or {}).get("profile") or "")


def seat_login(loop: dict, role: str) -> str:
    if role == "adjudicator":
        return ""
    if role == "triage":
        return triage_login(loop)
    if role == "issue_fixer":
        role = "fixer"
    return str(((loop.get("seats") or {}).get(role) or {}).get("login") or "")


DEFAULTS: dict = {
    "base": "main",
    "cap": 3,
    "fixers": [],
    "reviewers": [],
    "reviewer_seat": "",
    "seats": {},
    "adjudicator": {},
    "triage": {},
    "shadow": {},
    "observer": {},
    "skill": "",
    "read_token": "",
    "tokens": {},
    "apps": {},
    "clone": "",
    "roots": [],
    "concurrency": 1,         # runs allowed at once per seat; >1 requires isolation
    # How long a quiet head may sit before the watchdog speaks. It must cover a whole default
    # turn — 300 s prefetch + 900 s budget + 30 s kill grace + 600 s broker drain = 1830 s — so a
    # fresh install's healthy turn is never a "stall" (doctor checks it); 35 leaves a sweep's slack.
    "grace_min": 35,
    "marker_grace_min": 60,
    "cooldown_h": 6,
    "ttl_min": 45,            # seat lock lifetime: past this a crashed run has lost its seat
    "inflight_ttl_min": 10,
    "turn_budget_s": 900,     # wall clock for one isolated seat turn (seats.<seat>.turn_budget_s wins)
    "host": "",
    "unattended_fixer_push": False,  # per-repository; never inherited from plugin settings
    "attribution": True,      # sign what the loop posts (#197); false turns footer and trailer off
    "review_after_ci": False,  # hold a review while the head's checks are still running (#241)
    "merge_check": "auto",     # post the diaktoros/review merge check: auto|on|off (#1163 M2b)
    "ingress": "repo-hooks",   # where events arrive: repo-hooks|app-webhook (#1239)
    "ingress_app": "",         # with app-webhook: the App whose webhook delivers (its bot login)
    "fix_ci": False,          # hand a red required check on a fixer's PR to the fixer (#306)
    "fixer_check": "",        # one command fixer turns always run before publishing; "" = none
    "required_checks": [],    # the checks that gate an approval (#368); [] = every check gates
    "human_paths": [],        # globs whose changes only a human may approve (#478)
    "generated": [],          # generated files the host regenerates on a conflict (#551); [] = off
    "ci_coverage": {},        # what each required check covers, for the reviewer (#756); {} = off
    "review_only": [],        # authors reviewed but never fixed (#191); [] = fixers only
    "operator_agents": [],    # agents that post as the operator's account (#757); [] = none named
    "review_only_update": False,  # push a clean merge of the base into a review-only PR (opt-in)
    "review_only_cap": None,   # verdicts per review-only PR; None = the loop's `cap`
    "review_only_daily": None,  # reviewer turns a day on review-only PRs; None = no cap
    "ci_fix_cap": None,       # CI-fix turns per PR before the reviewer reviews red CI; None = 3
    "fixer_scope_lock": False,  # hold a fix round that changes files its findings do not name (#482)
}

def unattended_fixer_push_enabled(loop: dict) -> bool:
    """Only a literal opt-in in a trusted loop config authorizes unattended fixer pushes.

    Callers must load the loop from the host-owned config file, not an event payload or
    sandbox-supplied mapping. Hook arming, seat assignment and a legacy config are not consent.
    """
    return isinstance(loop, dict) and loop.get("unattended_fixer_push") is True


# While a loop has not opted in, a changes-requested verdict starts no fixer turn: the turn
# could not publish, so it would only spend a model conversation and fail. The verdict is held
# for the operator instead, and every surface (gate queue, explain, doctor, watchdog, observer)
# names the same reason and the same command. Held verdicts never create a ledger row, so an
# opt-in afterwards admits a *new* run at that moment; it never upgrades an older one (#22).
FIXER_PUSH_HOLD = "fixer held: unattended fixer pushes are off for this loop"


def fixer_push_enable_command(loop: dict) -> str:
    return f"hermes dk fixer-push --loop {loop['id']} --enable --acknowledge-pr-race"


def fixer_push_hold_reason(loop: dict) -> str:
    return (f"{FIXER_PUSH_HOLD} — the changes-requested verdict waits for you. To let the fixer "
            f"answer it, run `{fixer_push_enable_command(loop)}`; the next watchdog sweep (or "
            f"`hermes dk drain --loop {loop['id']} --seat fixer`) then starts the fix "
            "run for this head. Or fix it by hand, push, and re-request review.")


def is_fixer_push_hold(entry: object) -> bool:
    """Whether a seat-queue entry is the fixer's push-off hold.

    The reason may be the plain hold (``fixer_push_hold_reason``) or the shared ``broker_ipc``
    denial (#81) — both start with ``FIXER_PUSH_HOLD``'s opening words, so one prefix test
    covers both and keeps the hold idempotent across redelivery.
    """
    reason = str((entry or {}).get("reason") or "") if isinstance(entry, dict) else ""
    return isinstance(entry, dict) and reason.startswith(FIXER_PUSH_HOLD.split(" — ")[0])

SEAT_KEYS = ("reviewer", "fixer")

# One isolated turn's wall clock, in seconds. It is Hermes's ``--run-budget`` and (plus a short
# grace) the sandbox kill deadline. A real review reads the diff, builds and runs tests: two
# minutes (the old hard-coded worker value) cannot fit that, and a kill mid-push quarantines the run.
DEFAULT_TURN_BUDGET_S = 900
TURN_BUDGET_RANGE = (60, 14400)


def turn_budget(loop: dict, seat: str) -> int:
    """Seconds one isolated ``seat`` turn may run: the seat's own value, else the loop's.

    Tolerates an un-normalized loop dict (the worker and tests read raw configs): a missing value
    is the default, never zero. ``normalize`` is where a bad value is refused.
    """
    seat_cfg = (loop.get("seats") or {}).get(seat) or {}
    value = seat_cfg.get("turn_budget_s")
    if value is None or value == "":
        value = loop.get("turn_budget_s")
    if value is None or value == "":
        value = DEFAULT_TURN_BUDGET_S
    return int(value)


def _recorded_budget(recorded) -> int:
    """A claim's recorded budget as whole seconds; None is "none recorded" (0). Anything that
    is not a number is refused: a silently ignored value would be a silently wrong clock."""
    if recorded is None:
        return 0
    if (isinstance(recorded, bool) or not isinstance(recorded, (int, float))
            or not math.isfinite(recorded)):
        raise ValueError(f"recorded turn budget must be finite seconds, got {recorded!r}")
    return int(recorded)


def claim_budget(entry) -> float | None:
    """The budget a seat claim (a ``locks.json`` entry) recorded, or None — for a legacy or
    hand-edited claim whose value is missing or not a number, which then gets its seat's own
    clock rather than an error in a read-only report."""
    value = entry.get("budget") if isinstance(entry, dict) else None
    # NaN and infinity are floats too: a claim holding one gets its seat's clock, like any
    # other value that is not a number of seconds (#98).
    return (value if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) else None)


def turn_parts(loop: dict, seat: str | None = None, *, recorded: float | None = None) -> dict:
    """The pieces of one isolated turn's worst-case wall clock, launch to end, in seconds.

    Before the budget: the host dependency prefetch (``deps.FETCH_TIMEOUT``, #51). Then the
    budget — ``seat``'s, or the longest seat's — the sandbox kill grace after it
    (``trusted_turn.KILL_GRACE_S``), and the broker drain that lets an in-flight write (a push)
    finish once the sandbox is gone (``trusted_turn.BROKER_DRAIN_S``) — all inside the run, all
    before its seat is released. ``recorded`` is a budget a running turn was started with: the
    clock never runs shorter than it, whatever the loop says now (a row keeps its budget).
    """
    from .. import deps, trusted_turn   # imported late: both import this module
    seats = (seat,) if seat else (*SEAT_KEYS, "adjudicator")
    budget = max(max(turn_budget(loop, name) for name in seats), _recorded_budget(recorded))
    return {"prefetch": deps.FETCH_TIMEOUT, "budget": budget,
            "grace": trusted_turn.KILL_GRACE_S, "drain": trusted_turn.BROKER_DRAIN_S}


def worst_turn_s(loop: dict, seat: str | None = None, *, recorded: float | None = None) -> int:
    """Seconds one isolated turn may take from launch to end (see ``turn_parts``)."""
    return sum(turn_parts(loop, seat, recorded=recorded).values())


def seat_ttl_s(loop: dict, *, seat: str | None = None, recorded: float | None = None) -> int:
    """How long a ``seat``'s claim lives: ``ttl_min``, or that seat's whole worst-case turn if
    that is longer (the longest seat's when no seat is named).

    ``ttl_min`` is the backstop for a run that died without a verdict; it must never be what
    takes a slot from a turn that is still inside its own budget (#98) — and another seat's
    longer budget must not keep a dead claim alive either. ``recorded``, the budget the claim
    was taken with, keeps a lowered ``turn_budget_s`` from shortening it.
    """
    return max(int(loop.get("ttl_min") or DEFAULTS["ttl_min"]) * 60,
               worst_turn_s(loop, seat, recorded=recorded))


def seat_died_after_s(loop: dict, *, seat: str | None = None,
                      recorded: float | None = None) -> int:
    """Age past which the watchdog reports a seat claim as a run that died: twice its TTL.

    ``seat`` and ``recorded`` are keyword-only (#98): positionally a seat name could land in
    ``recorded`` and the clock would silently be the longest seat's."""
    return 2 * seat_ttl_s(loop, seat=seat, recorded=recorded)


def stall_grace_s(loop: dict, seat: str) -> int:
    """How long the watchdog lets ``seat``'s part of a PR sit quiet before calling it a stall:
    ``grace_min``, or that seat's whole worst-case turn if longer (#98). Per seat, so one
    seat's long budget never delays the stall report of another."""
    grace = int(loop.get("grace_min") or DEFAULTS["grace_min"]) * 60
    return max(grace, worst_turn_s(loop, seat))


def adjudicating_stall_s(loop: dict) -> int:
    """How long a breach marker may sit ``adjudicating`` — a ruling claimed — with no live
    adjudicator run before the watchdog calls the PR stalled: ``marker_grace_min``, or the
    adjudicator's whole worst-case turn if that is longer (#98). While the run is live, never."""
    grace = int(loop.get("marker_grace_min") or DEFAULTS["marker_grace_min"]) * 60
    return max(grace, worst_turn_s(loop, "adjudicator"))


def _check_budget(value, what: str, where: str) -> int:
    low, high = TURN_BUDGET_RANGE
    try:
        budget = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: {what} must be whole seconds, got {value!r}") from None
    if isinstance(value, bool) or not low <= budget <= high:
        raise ConfigError(f"{where}: {what} must be {low}-{high} seconds, got {value!r} — it is "
                          "the whole wall clock of one seat turn (build and tests included)")
    return budget


DAILY_TURNS_MAX = 1000
DAILY_TOKENS_MAX = 10_000_000_000

# Agent steps one isolated turn may take (#271), per role when the seat sets none. A seat that
# reads one diff and judges it fits the small default; one that must find its way into the code
# and change it, from a review or from an issue alone, needs room. The turn budget still bounds
# every turn's wall clock. Model calls follow from steps (``model_calls``).
DEFAULT_MAX_STEPS = {"reviewer": 60, "adjudicator": 40, "triage": 24,
                     "fixer": 80, "issue_fixer": 80}
# A shadow review (#218) runs with the reviewer's scope role, so it has no entry above; its own
# default is roomier, since another model may explore more before it submits (grok-4.7's first
# live shadow ran out at the reviewer's 60).
DEFAULT_SHADOW_MAX_STEPS = 100
MAX_STEPS_RANGE = (8, 200)


def _check_max_steps(value, what: str, where: str) -> int:
    low, high = MAX_STEPS_RANGE
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{where}: {what} must be a whole number of agent steps, {low}-{high} "
                          f"(leave it out for the seat's default), got {value!r}")
    return value


def max_steps(loop: dict, role: str) -> int:
    """Agent steps one ``role`` turn may take: the seat's own ``max_steps``, else the role's
    default. An issue fix runs as the fixer seat (#214), so it takes the fixer's value."""
    seat = "fixer" if role == "issue_fixer" else role
    value = (((loop.get("seats") or {}).get(seat)) or {}).get("max_steps")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_SHADOW_MAX_STEPS if role == "shadow" else DEFAULT_MAX_STEPS[role]


def model_calls(steps: int) -> int:
    """Model calls the turn's proxy grants for ``steps``: a margin above them, since a step can
    make more than one call (a retry, a context compression). 24 -> 32, 80 -> 100, 200 -> 250."""
    return steps + max(8, steps // 4)


FIXER_CHECK_MAX = 500
REQUIRED_CHECKS_MAX = 50
REQUIRED_CHECK_NAME_MAX = 100


MERGE_CHECK_NAME = "diaktoros/review"     # ci.MERGE_CHECK (config imports nothing from ci)


def check_required_checks(value, where: str) -> list[str]:
    """The operator's required checks (#368): unique, printable, one-line names, exactly as GitHub
    shows them; [] means every check gates. Host-owned: never read from a PR."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ConfigError(f"{where}: 'required_checks' must be a list of check names")
    names = [name.strip() for name in value]
    if (len(names) > REQUIRED_CHECKS_MAX or len(set(names)) != len(names)
            or not all(0 < len(name) <= REQUIRED_CHECK_NAME_MAX and name.isprintable()
                       for name in names)):
        raise ConfigError(f"{where}: 'required_checks' must be at most {REQUIRED_CHECKS_MAX} "
                          f"unique names, each one printable line of 1-{REQUIRED_CHECK_NAME_MAX} "
                          "characters")
    if any(name.lower() == MERGE_CHECK_NAME for name in names):
        # #1233: the loop never reads its own check as CI, so requiring it refuses every approval.
        raise ConfigError(f"{where}: 'required_checks' cannot name {MERGE_CHECK_NAME}, the loop's "
                          "own merge check; require it in the base branch's protection instead")
    return names


CI_COVERAGE_MAX = 50
CI_COVERS_MAX = 200


def check_ci_coverage(value, where: str) -> dict[str, str]:
    """The operator's map of check name -> what that check covers (#756), e.g. ``{"tests (3.14)":
    "full unit suite and harness"}``; {} = the reviewer is told nothing about it. Names are as
    GitHub shows them (no ``=`` or ``;;``, which the one-line forms use), the text one printable
    line. Host-owned: never read from a PR."""
    if value is None:
        return {}
    bad = f"{where}: 'ci_coverage' must map check names to a line saying what each covers"
    if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ConfigError(bad)
    out = {name.strip(): covers.strip() for name, covers in value.items()}
    if (len(value) > CI_COVERAGE_MAX or len(out) != len(value)
            or not all(0 < len(n) <= REQUIRED_CHECK_NAME_MAX and n.isprintable()
                       and "=" not in n and ";;" not in n for n in out)
            or not all(0 < len(c) <= CI_COVERS_MAX and c.isprintable() and ";;" not in c
                       for c in out.values())):
        raise ConfigError(f"{bad}: at most {CI_COVERAGE_MAX} unique names (1-"
                          f"{REQUIRED_CHECK_NAME_MAX} printable characters, no '=' or ';;'), "
                          f"each text 1-{CI_COVERS_MAX} printable characters, no ';;'")
    if any(name.lower() == MERGE_CHECK_NAME for name in out):
        raise ConfigError(f"{where}: 'ci_coverage' cannot name {MERGE_CHECK_NAME}, the loop's "
                          "own merge check, which the reviewer never reads as CI")
    return out


def parse_ci_coverage(entries, where: str) -> dict[str, str]:
    """``NAME=COVERS`` strings (the CLI and the form's spelling) -> ``ci_coverage``."""
    parsed: dict[str, str] = {}
    for text in entries:
        name, sep, covers = str(text).partition("=")
        if not sep:
            raise ConfigError(f"{where}: 'ci_coverage' entry {text!r} must be NAME=COVERS")
        if name.strip() in parsed:
            raise ConfigError(f"{where}: 'ci_coverage' names {name.strip()!r} twice")
        parsed[name.strip()] = covers
    return check_ci_coverage(parsed, where)


def split_ci_coverage(text) -> list[str]:
    """The settings form's one line: entries separated by ``;;``."""
    return [part.strip() for part in str(text or "").split(";;") if part.strip()]


def ci_coverage_text(coverage) -> str:
    """The inverse of ``split_ci_coverage`` (for ``show``)."""
    return " ;; ".join(f"{name}={covers}" for name, covers in (coverage or {}).items())


def ci_coverage(loop: dict) -> dict[str, str]:
    """The check name -> what it covers map; {} = off."""
    value = loop.get("ci_coverage")
    return dict(value) if isinstance(value, dict) else {}


HUMAN_PATHS_MAX = 50
HUMAN_PATH_MAX = 200


def check_human_paths(value, where: str) -> list[str]:
    """Glob patterns (#478) for paths only a human may approve: unique, printable, one line each.
    [] reserves nothing. Host-owned: never read from a PR."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ConfigError(f"{where}: 'human_paths' must be a list of glob patterns")
    names = [name.strip() for name in value]
    if (len(names) > HUMAN_PATHS_MAX or len(set(names)) != len(names)
            or not all(0 < len(name) <= HUMAN_PATH_MAX and name.isprintable() for name in names)):
        raise ConfigError(f"{where}: 'human_paths' must be at most {HUMAN_PATHS_MAX} unique "
                          f"patterns, each one printable line of 1-{HUMAN_PATH_MAX} characters")
    return names


GENERATED_MAX = 20           # entries, and paths per entry
GENERATED_PATH_MAX = 200
GENERATED_COMMAND_MAX = 500


def check_generated(value, where: str) -> list[dict]:
    """The loop's generated files (#551): ``[{"paths": [...], "command": "..."}]``, [] = off.

    Host-owned (the loop file), never read from a PR. Paths are repo-relative, with no ``..``,
    ``.git`` or empty segment and no comma or ``=`` (the one-line forms use both); a path may
    belong to one entry only. The command is one printable line."""
    if value is None:
        return []
    bad = f"{where}: 'generated' must be a list of {{\"paths\": [...], \"command\": \"...\"}}"
    if not isinstance(value, list) or len(value) > GENERATED_MAX:
        raise ConfigError(f"{bad}, at most {GENERATED_MAX}")
    out, seen = [], set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"paths", "command"}:
            raise ConfigError(bad)
        paths, command = entry["paths"], entry["command"]
        if (not isinstance(paths, list) or not 1 <= len(paths) <= GENERATED_MAX
                or not all(isinstance(p, str) for p in paths) or not isinstance(command, str)):
            raise ConfigError(bad)
        paths = [p.strip() for p in paths]
        command = command.strip()
        for path in paths:
            parts = path.split("/")
            if (not 0 < len(path) <= GENERATED_PATH_MAX or not path.isprintable()
                    or path.startswith("/") or "\\" in path or "," in path or "=" in path
                    or any(part in ("", ".", "..") or part.lower() == ".git" for part in parts)):
                raise ConfigError(f"{where}: 'generated' path {path!r} must be repo-relative "
                                  "(no '..', '.git', empty segment, comma or '=')")
            if path in seen:
                raise ConfigError(f"{where}: 'generated' path {path!r} is listed twice")
            seen.add(path)
        if not command or len(command) > GENERATED_COMMAND_MAX or not command.isprintable():
            raise ConfigError(f"{where}: a 'generated' command must be one printable line of "
                              f"1-{GENERATED_COMMAND_MAX} characters")
        out.append({"paths": paths, "command": command})
    return out


def parse_generated(entries, where: str) -> list[dict]:
    """``PATH[,PATH...]=COMMAND`` strings (the CLI and the form's spelling) -> ``generated``."""
    parsed = []
    for text in entries:
        left, sep, command = str(text).partition("=")
        if not sep:
            raise ConfigError(f"{where}: 'generated' entry {text!r} must be PATH[,PATH...]=COMMAND")
        parsed.append({"paths": [p.strip() for p in left.split(",") if p.strip()],
                       "command": command})
    return check_generated(parsed, where)


def split_generated(text) -> list[str]:
    """The settings form's one line: entries separated by ``;;``."""
    return [part.strip() for part in str(text or "").split(";;") if part.strip()]


def generated_text(entries) -> str:
    """The inverse of ``split_generated`` (for ``show``)."""
    return " ;; ".join(f"{','.join(e['paths'])}={e['command']}" for e in entries or [])


def generated(loop: dict) -> list[dict]:
    """The generated-file entries; [] = the conflict shortcut is off."""
    value = loop.get("generated")
    return list(value) if isinstance(value, list) else []


def split_human_paths(text) -> list[str]:
    """A one-line list of glob patterns, comma-separated."""
    return [name.strip() for name in str(text or "").split(",") if name.strip()]


def human_paths(loop: dict) -> list[str]:
    """The globs whose changes the loop may not approve; [] = none."""
    names = loop.get("human_paths")
    return list(names) if isinstance(names, list) else []


def human_path_hits(loop: dict, paths) -> list[str]:
    """The ``paths`` that match a ``human_paths`` glob, in order, without repeats.

    ``fnmatch`` semantics, case-sensitive: ``*`` also crosses ``/``, so ``.github/*`` and
    ``.github/**`` both cover everything below ``.github``."""
    import fnmatch
    patterns = human_paths(loop)
    hits: list[str] = []
    for path in paths:
        if path not in hits and any(fnmatch.fnmatchcase(path, pat) for pat in patterns):
            hits.append(path)
    return hits


def split_check_names(text) -> list[str]:
    """A one-line list of check names: commas separate them, except inside parentheses, which
    GitHub matrix names use (``test (ubuntu-24.04, 3.11)``)."""
    names, depth, current = [], 0, []
    for ch in str(text or ""):
        if ch == "," and depth == 0:
            names.append("".join(current))
            current = []
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0)
        depth = max(depth, 0)
        current.append(ch)
    names.append("".join(current))
    return [name.strip() for name in names if name.strip()]


SKILL_PREFIX, OLD_SKILL_PREFIX = "diaktoros:", "hermes-review-loop:"
REVIEW_ONLY_MAX = 50
_LOGIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38})\Z")


def _check_logins(value, key: str, where: str) -> list[str]:
    """``fixers`` / ``reviewers``: a list of logins, lowercased. A bare string would be read one
    character at a time (``"rev-bot"`` -> ``r``, ``e``, ...), so any non-list is refused."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ConfigError(f"{where}: {key!r} must be a list of GitHub logins")
    return [name.lower() for name in value]


def check_review_only(value, loop: dict, where: str) -> list[str]:
    """Review-only authors (#191): GitHub logins, lowercased, unique, and neither a fixer nor a
    reviewer — a fixer's PR is fixed, and a reviewer never reviews its own."""
    names = _check_login_list(value, "review_only", where)
    clash = sorted(set(names) & (set(loop.get("fixers") or []) | set(loop.get("reviewers") or [])))
    if clash:
        raise ConfigError(f"{where}: {', '.join(clash)} cannot be review-only and also a fixer "
                          "or a reviewer")
    return names


def check_operator_agents(value, loop: dict, where: str) -> list[str]:
    """``operator_agents`` (#757): logins of agents that post as the operator's own account.
    Lowercased, unique, and none of them a seat's account: a seat speaks for the loop, not for
    the operator, and one login can't be both."""
    names = _check_login_list(value, "operator_agents", where)
    clash = sorted(set(names) & seat_accounts(loop))
    if clash:
        raise ConfigError(f"{where}: {', '.join(clash)} cannot be an operator agent and also a "
                          "fixer, a reviewer or another seat's account")
    return names


def operator_agents(loop: dict) -> set[str]:
    """Logins that speak with maintainer authority but are agents, not the operator in person
    (``operator_agents``, #757). Only a label: the authority is ``triage.maintainers``."""
    names = loop.get("operator_agents")
    return {str(name).lower() for name in names} if isinstance(names, list) else set()


def maintainer_kind(loop: dict, login: str) -> str:
    """How a maintainer is named wherever the loop shows one (#757): ``operator agent`` for a
    login in ``operator_agents``, else ``maintainer``. The one place that wording lives."""
    return "operator agent" if str(login).lower() in operator_agents(loop) else "maintainer"


def _check_login_list(value, key: str, where: str) -> list[str]:
    """A list of GitHub logins, lowercased, unique, at most ``REVIEW_ONLY_MAX`` (``review_only``
    and ``operator_agents``)."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise ConfigError(f"{where}: {key!r} must be a list of GitHub logins")
    # A login never contains a comma, so "a,b" is two logins on every path: the settings form
    # always split them, and `--review-only a,b` now does too (it used to fail as one login).
    parts = [part.strip().lower() for name in value for part in name.split(",")]
    names = [part for part in parts if part]
    bad = [name for name in names if not _LOGIN.match(name)]
    twice = sorted({name for name in names if names.count(name) > 1})
    problems = (["an empty entry (a blank login or a stray comma)"] if len(names) < len(parts)
                else []) + \
        ([f"not a GitHub login: {', '.join(repr(n) for n in bad)}"] if bad else []) + \
        ([f"listed twice: {', '.join(twice)}"] if twice else []) + \
        ([f"{len(names)} logins, more than {REVIEW_ONLY_MAX}"] if len(names) > REVIEW_ONLY_MAX
         else [])
    if problems:
        raise ConfigError(f"{where}: {key!r} must be at most {REVIEW_ONLY_MAX} unique "
                          f"GitHub logins ({'; '.join(problems)})")
    return names


def seat_accounts(loop: dict) -> set[str]:
    """Every GitHub account a seat of this loop acts as (lowercased): the fixers, the reviewers
    and each seat's login. Their commits should come from a seat turn only (#757)."""
    names = set(loop.get("fixers") or []) | set(loop.get("reviewers") or [])
    if isinstance(loop.get("seats"), dict):
        names |= {seat_login(loop, role) for role in ("reviewer", "fixer", "triage")}
        names.add(adjudicator_login(loop))
    names.add(str(loop.get("reviewer_seat") or ""))
    return {str(name).strip().lower() for name in names if str(name).strip()}


def _check_review_only_limit(value, key: str, where: str) -> int | None:
    """``review_only_cap`` / ``review_only_daily``: a positive whole number, or empty (unset)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        value = _form_int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= DAILY_TURNS_MAX:
        raise ConfigError(f"{where}: {key} must be a whole number, 1-{DAILY_TURNS_MAX} "
                          f"(leave it empty for the default), got {value!r}")
    return value


def review_only_cap(loop: dict) -> int:
    """Verdicts the reviewer may give one review-only PR: ``review_only_cap``, else ``cap``."""
    value = loop.get("review_only_cap")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return int(loop["cap"])


CI_FIX_CAP_DEFAULT = 3
CI_FIX_CAP_MAX = 10


def check_ci_fix_cap(value, where: str) -> int | None:
    """``ci_fix_cap`` (#539): a whole number 1-10, or empty (unset: the default, 3)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        value = _form_int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= CI_FIX_CAP_MAX:
        raise ConfigError(f"{where}: ci_fix_cap must be a whole number, 1-{CI_FIX_CAP_MAX} "
                          f"(leave it empty for {CI_FIX_CAP_DEFAULT}), got {value!r}")
    return value


def ci_fix_cap(loop: dict) -> int:
    """CI-fix turns one PR gets before a red head waits for you (#539)."""
    value = loop.get("ci_fix_cap")
    ok = isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= CI_FIX_CAP_MAX
    return value if ok else CI_FIX_CAP_DEFAULT


def review_only_daily(loop: dict) -> int | None:
    """Reviewer turns per local day on review-only PRs, or None (no cap)."""
    value = loop.get("review_only_daily")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def review_only(loop: dict) -> set[str]:
    """Authors whose PRs are reviewed but never handed to the fixer (#191)."""
    names = loop.get("review_only")
    return {str(name).lower() for name in names} if isinstance(names, list) else set()


def reviewed_authors(loop: dict) -> set[str]:
    """Every author whose PRs the reviewer serves: the fixers and the review-only authors."""
    return set(loop.get("fixers") or []) | review_only(loop)


def required_checks(loop: dict) -> list[str]:
    """The checks that gate an approval, the CI hold and the fixer hand-off; [] = all of them."""
    names = loop.get("required_checks")
    return list(names) if isinstance(names, list) else []


def check_fixer_check(value, where: str) -> str:
    """The loop's always-run check: one printable line, at most ``FIXER_CHECK_MAX`` characters,
    or "" for none. The operator's own text; it reaches the fixer as an instruction."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{where}: 'fixer_check' must be a string (one command, or \"\")")
    value = value.strip()
    if len(value) > FIXER_CHECK_MAX or not all(ch.isprintable() for ch in value):
        raise ConfigError(f"{where}: 'fixer_check' must be one printable line of at most "
                          f"{FIXER_CHECK_MAX} characters (chain commands with &&)")
    return value


def _check_daily_turns(value, what: str, where: str) -> int:
    """A per-seat daily turn cap (#219): a whole number of turns, 1–DAILY_TURNS_MAX."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= DAILY_TURNS_MAX:
        raise ConfigError(f"{where}: {what} must be a whole number of turns, 1-{DAILY_TURNS_MAX} "
                          f"(leave it out for no cap), got {value!r}")
    return value


def _check_daily_tokens(value, what: str, where: str) -> int:
    """A per-seat daily token budget (#480): a whole number of tokens, 1-DAILY_TOKENS_MAX."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= DAILY_TOKENS_MAX:
        raise ConfigError(f"{where}: {what} must be a whole number of tokens, "
                          f"1-{DAILY_TOKENS_MAX} (leave it out for no budget), got {value!r}")
    return value


def seat_daily_tokens(loop: dict, seat: str) -> int | None:
    """Tokens (in + out) ``seat`` may spend per local day on this loop, or None (#480). A turn
    in flight is never killed; once the budget is spent the next turn is held until midnight.
    An issue fix spends the fixer's budget."""
    seat = "fixer" if seat == "issue_fixer" else seat
    value = (((loop.get("seats") or {}).get(seat)) or {}).get("daily_tokens")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def seat_daily_turns(loop: dict, seat: str) -> int | None:
    """Turns ``seat`` may start per local day on this loop, or None (no cap) (#219).

    Issue fixes are always capped (#247): ``triage.fix_daily_turns``, else
    ``DEFAULT_FIX_DAILY_TURNS``. They have no persisted seat of their own."""
    if seat == "issue_fixer":
        value = (loop.get("triage") or {}).get("fix_daily_turns")
        return (value if isinstance(value, int) and not isinstance(value, bool) and value > 0
                else DEFAULT_FIX_DAILY_TURNS)
    value = (((loop.get("seats") or {}).get(seat)) or {}).get("daily_turns")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def isolated_runtime_configured() -> bool:
    """Whether this host has the isolated worker's runtime file, so every turn runs isolated.

    The one place that decides when parallel runs need no ``clone``. Production turns are only
    launched by the isolated worker (``gate.enqueue_isolated``), which gives each its own
    exact-head export and sandbox and never a shared writable checkout; without this file it
    cannot start (``gate.resume_isolated`` checks the same file), and ``clone`` stays the only
    isolation source. Presence only: ``doctor`` and ``selftest`` check the file is valid. The
    legacy shared-checkout path (``gate.take_seat``) does not rely on this: it queues a run it
    cannot isolate above concurrency 1 whatever is configured.
    """
    return host_path("runtime").is_file()


def seat_concurrency(loop: dict, seat: str) -> int:
    """How many PRs this seat may work at once.

    A seat's own ``concurrency`` wins; the loop-level value is the default for every seat; and 1
    (serialized) is the fallback. Per seat is the shape that matters: a fixer and a reviewer are
    different models on different budgets, and wanting two reviews in flight rarely means wanting
    two fixes in flight.
    """
    seat_cfg = (loop.get("seats") or {}).get(seat) or {}
    value = seat_cfg.get("concurrency")
    if (value is None or value == "") and seat not in ("adjudicator", "triage", "issue_fixer",
                                                       "shadow"):
        # The loop default is documented as the default for the two *working* seats. A ruling
        # is rare and read-only; it never inherits a parallelism the operator chose for reviews.
        value = loop.get("concurrency", 1)
    if value is None or value == "":
        value = 1
    return int(value)


def adjudicator_login(loop: dict) -> str:
    """The optional GitHub identity the adjudicator comments as, or ``""`` for operator-only.

    Unlike the two working seats the adjudicator needs no GitHub identity at all: a ruling is
    always delivered to the operator and recorded in the host ledger. Only when this is set is
    the ruling *also* posted on the PR, as this login and never as a seat or the reader.
    """
    return str((((loop.get("seats") or {}).get("adjudicator")) or {}).get("login") or "")


# -- issue triage (#213) -----------------------------------------------------------------------

# A triage run has no PR head; the ledger's head column holds this marker instead, and the
# run is keyed by the issue number (``pr``) with seat ``triage`` and turn key ``triage``.
TRIAGE_HEAD = "issue"
TRIAGE_KEYS = {"route", "profile", "authors", "labels", "max_labels", "comment", "login",
               "fix_label", "maintainers", "fix_daily_turns", "auto_fix_labels",
               "auto_fix_daily"}
# Automatic issue fixes per loop per local day when the loop sets none (#232).
DEFAULT_AUTO_FIX_DAILY = 25
# Priorities a person owns: an issue carrying one is never auto-offered (#232).
AUTO_FIX_BLOCKED = re.compile(r"P[012]\Z", re.IGNORECASE)
# The deepest filed-issue lineage (#247) still auto-offered: past it a person must hand it over.
AUTO_FIX_MAX_DEPTH = 2
# Issue-fix turns per local day when the loop sets none (#247): the chain bound. Every issue fix
# opens a new PR, so the per-PR verdict cap never limits how many happen; this does.
DEFAULT_FIX_DAILY_TURNS = 10
# An issue-fix run (#214) is keyed by the issue and the base commit it starts from (``head``);
# it pushes only to this branch, created fresh for the issue.
ISSUE_FIX_BRANCH = "diaktoros/issue-{number}"
# The branch an issue fix opened before the rename (#425): its PR is still an issue fix's PR.
ISSUE_FIX_BRANCH_RE = r"(?:diaktoros|review-loop)/issue-(\d+)"
TRIAGE_LABEL = re.compile(r"[^\x00-\x1f,{}`]{1,50}\Z")
TRIAGE_MAX_LABELS = 10
TRIAGE_LABELS_MAX = 100


def triage_enabled(loop: dict) -> bool:
    return bool((loop.get("triage") or {}).get("route"))


def triage_login(loop: dict) -> str:
    """The account triage labels (and comments) as: its own, else the reviewer seat's."""
    triage = loop.get("triage") or {}
    if not triage.get("route"):
        return ""
    return str(triage.get("login") or ((loop.get("seats") or {}).get("reviewer") or {}).get("login")
               or "").lower()


# The conditions ``issue_fixes_enabled`` needs: (key, what is wrong when it fails).
# ``issue_fixes_off`` is the one place that tests them.
ISSUE_FIX_CONDITIONS = (("triage", "triage is off"),
                        ("fix_label", "no fix label is set"),
                        ("push", "unattended fixer pushes are off"))


def issue_fixes_off(loop: dict) -> tuple[str, ...]:
    """The ``ISSUE_FIX_CONDITIONS`` keys that fail for this loop; empty when issue fixes are on."""
    ok = {"triage": triage_enabled(loop),
          "fix_label": bool((loop.get("triage") or {}).get("fix_label")),
          "push": unattended_fixer_push_enabled(loop)}
    return tuple(key for key, _ in ISSUE_FIX_CONDITIONS if not ok[key])


def issue_fixes_enabled(loop: dict) -> bool:
    """Whether a maintainer's ``fix_label`` hands an issue to the fixer (#214) — needs triage on
    and the repository's unattended fixer pushes, which the operator opts into separately."""
    return not issue_fixes_off(loop)


def auto_fix_labels(loop: dict) -> set[str]:
    """The triage labels (casefolded) that hand an issue to the fixer automatically (#232)."""
    if not issue_fixes_enabled(loop):
        return set()
    return {str(x).casefold() for x in (loop.get("triage") or {}).get("auto_fix_labels") or []}


def auto_fix_daily(loop: dict) -> int:
    """Automatic issue fixes per loop per local day: ``triage.auto_fix_daily``, else 25."""
    value = (loop.get("triage") or {}).get("auto_fix_daily")
    return (value if isinstance(value, int) and not isinstance(value, bool) and value > 0
            else DEFAULT_AUTO_FIX_DAILY)


def auto_fix_eligible(loop: dict, present) -> bool:
    """Whether an issue with the labels ``present`` may be auto-offered: one of the
    ``auto_fix_labels`` and none of P0/P1/P2 (#232)."""
    names = {str(x).casefold() for x in present}
    return bool(names & auto_fix_labels(loop)) and not any(AUTO_FIX_BLOCKED.match(x) for x in names)


def maintainers(loop: dict) -> set[str]:
    """The loop's maintainers (``triage.maintainers``, lowercased), trusted to direct it: hand an
    issue to the fixer (#214) and ask the reviewer for a fresh review (#375), plus the
    ``operator_agents`` (#757): agents that post as the operator's account and speak with the
    same authority. Empty without either."""
    names = (loop.get("triage") or {}).get("maintainers") or []
    return {str(name).strip().lower() for name in names if str(name).strip()} | operator_agents(loop)


def hook_roles(loop: dict) -> tuple[str, ...]:
    """The roles with a repo hook on this loop: both seats, and triage when it is on. None when
    its events arrive through a GitHub App's webhook (#1239): the App's route wakes them all."""
    if app_ingress(loop):
        return ()
    return ("reviewer", "fixer") + (("triage",) if triage_enabled(loop) else ())


# Shadow reviews (#218): one in this many reviewer turns also gets a no-write review by the
# shadow profile's model, unless the loop says otherwise.
DEFAULT_SHADOW_EVERY = 5
SHADOW_EVERY_RANGE = (1, 1000)


def shadow_profile(loop: dict) -> str:
    """The Hermes profile shadow reviews run as (#218), or ``""`` when the loop has none (off)."""
    return str((loop.get("shadow") or {}).get("profile") or "")


def shadow_every(loop: dict) -> int:
    """Shadow one in this many reviewer turns (#218); 1 shadows every one."""
    value = (loop.get("shadow") or {}).get("every")
    return (value if isinstance(value, int) and not isinstance(value, bool) and value >= 1
            else DEFAULT_SHADOW_EVERY)


def shadow_escalate(loop: dict) -> bool:
    """Whether a shadow review holds the PR (#1004): no ready-to-merge while the shadow review of
    the head is still running, or after it requested changes. Off unless the loop opts in."""
    return (loop.get("shadow") or {}).get("escalate") is True


def shadow_file_issues(loop: dict) -> bool:
    """Whether the host files the issues a shadow review asked for (#914), once the real review
    exists and did not file them. Off unless the loop opts in."""
    return (loop.get("shadow") or {}).get("file_issues") is True


# At most this many shadow-found issues a day per loop (#914): a second model's issue-tier output
# is useful, and unbounded it is noise.
SHADOW_ISSUES_DAILY = 5


APP_LOGIN = re.compile(r"[a-z0-9][a-z0-9-]{0,33}\[bot\]\Z")


def is_app(loop: dict, login: str) -> bool:
    """Whether ``login`` is mapped as a GitHub App identity (#1163), not a PAT."""
    name = str(login or "").lower()
    pats = {str(k).lower() for k in loop.get("tokens") or {}}
    return bool(name) and name not in pats and name in {str(k).lower() for k in loop.get("apps") or {}}


# What the reader needs to see each part of the repository's setup (#1241): a PAT's scope, or a
# GitHub App's repository permission (an App answers "Resource not accessible by integration").
_READER_ACCESS = {"hooks": ("hook read access (`repo`, or the narrower `read:repo_hook`)",
                            "Webhooks: Read"),
                  "protection": ("admin access to read classic branch protection (classic `repo` "
                                 "on an admin account)", "Administration: Read")}


def reader_access_need(loop: dict, what: str) -> str:
    """What the reader needs to see ``what``, for its kind of identity (#1255): a PAT's scope or
    an App's permission, for a detail line that says why a read was denied."""
    pat, app = _READER_ACCESS[what]
    return f"the repository permission {app}" if is_app(loop, str(loop.get("read_token") or "")) \
        else pat


def reader_access_fix(loop: dict, what: str) -> str:
    """The fix line for a reader that cannot see ``what`` ("hooks" or "protection"), worded for
    the reader's kind of identity: an App gets the permission to add, a PAT the scope."""
    pat, app = _READER_ACCESS[what]
    reader = str(loop.get("read_token") or "the reader")
    if is_app(loop, reader):
        return (f"give the reader App ({reader}) the repository permission {app}, then accept the "
                "new permission on its installation")
    return f"give the read token ({reader}) {pat}"


def reviewer_is_app(loop: dict) -> bool:
    """Whether the reviewer seat is a GitHub App identity (#1163). GitHub cannot request an App
    bot as a reviewer (a request answers 201 and does nothing, docs/github-app-support.md), so the
    handoff to it is delivered to the reviewer gate by the host instead."""
    seat = str(loop.get("reviewer_seat") or "").lower()
    pats = {str(k).lower() for k in loop.get("tokens") or {}}
    return bool(seat) and seat not in pats and seat in {str(k).lower() for k in loop.get("apps") or {}}


def credentials_map(loop: dict) -> dict:
    """Every identity's credential file: PAT files (``tokens``) and App private keys (``apps``).
    For "has a credential" and "uses its own file" checks only; a key is never read as a token."""
    found = {str(k): str(v or "") for k, v in (loop.get("tokens") or {}).items()}
    for login, spec in (loop.get("apps") or {}).items():
        if isinstance(spec, dict):
            found.setdefault(str(login), str(spec.get("key_file") or ""))
    return found


def check_concurrency(value, what: str, where: str) -> int:
    """A seat's own ``concurrency``: a whole number >= 1 (1 = serialized). The one rule the
    loader, ``set``, ``init``, ``setup`` and the settings form share for ``seats.<seat>.concurrency``."""
    number = _as_int(value, what, where)
    if number < 1:
        raise ConfigError(f"{where}: {what} must be >= 1 (1 = serialized)")
    return number


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKLY = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun) ([01]\d|2[0-3]):([0-5]\d)$")


def check_weekly_report(value) -> str:
    """``observer.weekly_report`` (#658): a weekday and a UTC time, ``mon 09:00``; else
    ValueError. Normalized to lower case with a single space."""
    text = " ".join(str(value).split()).lower() if isinstance(value, str) else ""
    if not _WEEKLY.match(text):
        raise ValueError(f"{value!r} is not a weekday and a UTC time like 'mon 09:00' "
                         f"(days: {', '.join(WEEKDAYS)})")
    return text


class ConfigError(Exception):
    """A loop file that cannot be trusted to drive a run."""


FOUR_IDENTITY_RULE = ("the four-identity rule: the reader, the reviewer, the fixer and (if set) "
                      "the adjudicator comment login must be four different accounts with four "
                      "different token files (docs/operations.md#token-files-one-pat-per-account)")


_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def webhook_host(value: str | None, *, required: bool = False) -> str:
    """Accept only a gateway origin that urllib.request will send to that authority."""
    host = str(value or "").strip()
    if not host:
        if required:
            raise ConfigError("webhook host required to install routes/hooks: pass --host https://your-gateway.example "
                              "or set your own webhook host in the plugin settings")
        return ""
    try:
        # urlsplit alone accepts empty userinfo/ports and percent-escaped host delimiters.
        # urllib.request then unquotes parts of the authority, so inspect the raw syntax too.
        parsed = urlsplit(host)
        authority = parsed.netloc
        valid = (parsed.scheme in ("http", "https")
                 and host[:len(parsed.scheme) + 3].lower() == parsed.scheme + "://"
                 and bool(authority) and parsed.path in ("", "/")
                 and "?" not in host and "#" not in host
                 and not any(ord(char) < 33 or ord(char) == 127 for char in host)
                 and not any(char in authority for char in "@%\\"))
        if valid:
            if authority.startswith("["):
                bracket = authority.find("]")
                ipaddress.IPv6Address(authority[1:bracket])
                suffix = authority[bracket + 1:]
            else:
                address, sep, port_text = authority.partition(":")
                suffix = sep + port_text
                if not address or len(address.rstrip(".")) > 253:
                    valid = False
                elif re.fullmatch(r"[0-9.]+", address):
                    ipaddress.IPv4Address(address)
                else:
                    labels = address.removesuffix(".").split(".")
                    valid = all(_DNS_LABEL.fullmatch(label) for label in labels)
            if suffix:
                valid = (valid and suffix.startswith(":") and bool(suffix[1:])
                         and suffix[1:].isascii() and suffix[1:].isdecimal()
                         and 1 <= int(suffix[1:]) <= 65535)
            valid = valid and parsed.hostname is not None
            if suffix and valid:
                valid = parsed.port is not None
        if valid:
            origin = host.removesuffix("/")
            request = Request(f"{origin}/webhooks/test")
            valid = (request.type == parsed.scheme and request.host == authority
                     and request.selector == "/webhooks/test")
    except (ValueError, IndexError):
        valid = False
    if not valid:
        raise ConfigError(f"invalid webhook host {value!r}: --host must be an http(s) gateway "
                          "origin without a path, query, or credentials")
    return host.removesuffix("/")


def home() -> pathlib.Path:
    from .host_guards import guard_real_home   # here, not module level: one binding (#676)
    return guard_real_home(pathlib.Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser())


# The host files this plugin keeps under $HERMES_HOME, as (new name, name before the rename).
# Until `migrate` moves an install's files (#425 stage 4), each is used where it already is; a
# fresh install gets the new name. Every reader and writer goes through ``host_path``, so one
# process never splits a file across the two names.
HOST_FILES = {
    "config_dir": ("diaktoros.d", "review-loops.d"),
    "runtime": ("diaktoros-runtime.json", "review-loop-runtime.json"),
    "ledger": ("state/diaktoros-runs.sqlite", "state/review-loop-runs.sqlite"),
    "state_root": ("state/diaktoros", "state/review-loops"),
    "gate_failures": ("state/diaktoros-gate-failures", "state/review-loop-gate-failures"),
    "pacing": ("state/diaktoros-pacing.json", "state/review-loop-pacing.json"),
    "seat_locks": ("state/diaktoros-seat-locks", "state/review-loop-seat-locks"),
    "watchdog_shim": ("scripts/diaktoros-watchdog.py", "scripts/review-loop-watchdog.py"),
}


def host_path(key: str, base: pathlib.Path | None = None) -> pathlib.Path:
    """The path of host file ``key`` under ``base`` (default ``home()``): the new name, unless
    only the old one exists."""
    new, old = HOST_FILES[key]
    root = home() if base is None else base
    if os.path.lexists(root / old) and not os.path.lexists(root / new):
        return root / old
    return root / new


# The shared watchdog job is named after the shim it runs: the two move together.
WATCHDOG_JOBS = {"diaktoros-watchdog.py": "diaktoros watchdog",
                 "review-loop-watchdog.py": "review loop watchdog"}


def watchdog_shim() -> pathlib.Path:
    """The cron shim the shared watchdog job runs (the old name until ``migrate`` moves it)."""
    return host_path("watchdog_shim")


def watchdog_job_name() -> str:
    return WATCHDOG_JOBS[watchdog_shim().name]


def default_state_dir(loop_id: str) -> pathlib.Path:
    return host_path("state_root") / loop_id


def is_default_state_dir(path, loop_id: str) -> bool:
    """Whether ``path`` is ``loop_id``'s default state directory under either name."""
    return any(pathlib.Path(str(path)) == home() / root / loop_id
               for root in HOST_FILES["state_root"])


def config_dir() -> pathlib.Path:
    from .host_guards import guard_real_home   # here, not module level: one binding (#676)
    override = envnames.get("CONFIG_DIR")
    return guard_real_home(pathlib.Path(override).expanduser()) if override else host_path("config_dir")


@contextlib.contextmanager
def push_policy_lock():
    """Serialize host CLI policy writes and a broker's final push attempt.

    Manual config edits outside this lock are not an authorization mechanism.
    """
    import fcntl
    from .. import hostdirs
    directory = hostdirs.ensure(config_dir())
    with (directory / '.fixer-push-policy.lock').open('a+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _path(value: str) -> pathlib.Path:
    return pathlib.Path(str(value)).expanduser()


def relative_path_problem(value) -> str:
    """Why ``value`` is not an absolute path (after ``~`` expands), or ``""`` when it is.

    The one wording every token-path check uses: ``normalize`` at load and
    ``credentials.token_file_problem`` for the write path and doctor.
    """
    raw = str(value or "").strip()
    if raw and not _path(raw).is_absolute():
        return f"{raw!r} is not an absolute path (use /… or ~/…)"
    return ""


def normalize(raw: dict, source: pathlib.Path | None = None) -> dict:
    """Fill defaults, expand paths, and refuse anything that would make a run ambiguous."""
    from . import blocks   # here, not module level (#676)
    if not isinstance(raw, dict):
        raise ConfigError(f"{source or 'config'}: expected a JSON object")
    loop = {**DEFAULTS, **raw}
    where = f"{source or loop.get('id', '<inline>')}"
    blocks.check_known_keys(raw, where)
    flags.check(loop, where)
    loop["fixer_check"] = check_fixer_check(loop["fixer_check"], where)
    loop["required_checks"] = check_required_checks(loop["required_checks"], where)
    loop["human_paths"] = check_human_paths(loop["human_paths"], where)
    loop["generated"] = check_generated(loop["generated"], where)
    loop["ci_coverage"] = check_ci_coverage(loop["ci_coverage"], where)
    if loop.get("ingress") not in INGRESS_MODES:
        raise ConfigError(f"{where}: 'ingress' must be one of {', '.join(INGRESS_MODES)}")
    loop["ingress_app"] = str(loop.get("ingress_app") or "").strip()
    if loop["ingress"] == "app-webhook":
        app = loop["ingress_app"].lower()
        if not APP_LOGIN.match(app) or app not in {str(k).lower() for k in loop.get("apps") or {}}:
            raise ConfigError(f"{where}: 'ingress' app-webhook needs 'ingress_app': the bot login "
                              "of a GitHub App mapped in 'apps' (its webhook delivers this loop's "
                              "events)")
    if loop.get("merge_check") not in MERGE_CHECK_MODES:
        raise ConfigError(f"{where}: 'merge_check' must be one of {', '.join(MERGE_CHECK_MODES)} "
                          "(auto: on when the reviewer is a GitHub App)")

    repo = str(loop.get("repo") or "").strip()
    if repo.count("/") != 1:
        raise ConfigError(f"{where}: 'repo' must be 'owner/name', got {repo!r}")

    loop["id"] = str(loop.get("id") or repo.split("/")[-1]).strip()
    loop["repo"] = repo.lower()
    loop["fixers"] = _check_logins(loop.get("fixers"), "fixers", where)
    loop["reviewers"] = _check_logins(loop.get("reviewers"), "reviewers", where)
    if not loop["fixers"]:
        raise ConfigError(f"{where}: 'fixers' must list at least one GitHub login")
    if not loop["reviewers"]:
        raise ConfigError(f"{where}: 'reviewers' must list at least one GitHub login")
    loop["review_only"] = check_review_only(loop.get("review_only"), loop, where)
    # A plugin skill is qualified by the plugin's name, and the plugin was renamed (#425): a loop
    # written before still names the old one, which no longer exists. Read it as the new name.
    if isinstance(loop.get("skill"), str) and loop["skill"].startswith(OLD_SKILL_PREFIX):
        loop["skill"] = SKILL_PREFIX + loop["skill"][len(OLD_SKILL_PREFIX):]

    raw_seats = loop.get("seats") if isinstance(loop.get("seats"), dict) else {}
    seats = {}
    if not isinstance(loop.get("seats"), dict):
        raise ConfigError(f"{where}: 'seats' must be an object with reviewer and fixer")
    for seat in SEAT_KEYS:
        if not isinstance(loop["seats"].get(seat) or {}, dict):
            raise ConfigError(f"{where}: seats.{seat} must be an object")
        seat_cfg = dict(loop["seats"].get(seat) or {})
        if not seat_cfg.get("route"):
            raise ConfigError(f"{where}: seats.{seat}.route is required")
        if not seat_cfg.get("profile"):
            raise ConfigError(f"{where}: seats.{seat}.profile is required")
        seat_cfg.setdefault("login", loop["reviewers"][0] if seat == "reviewer"
                            else (loop["fixers"][0] if len(loop["fixers"]) == 1 else ""))
        seat_cfg.setdefault("agent", seat_cfg["profile"].capitalize())
        seats[seat] = seat_cfg
    loop["seats"] = seats
    if str(seats["reviewer"]["route"]) == str(seats["fixer"]["route"]):
        # One route for two seats: one hook would wake (and "arm") both, and the gate script
        # bound to it can only be one of the two. Refused on load, not only at init/apply.
        raise ConfigError(f"{where}: seats.reviewer.route and seats.fixer.route are both "
                          f"{seats['reviewer']['route']!r} — each seat needs its own route")

    loop["reviewer_seat"] = str(loop.get("reviewer_seat")
                               or seats["reviewer"].get("login") or "").lower()
    if not loop["reviewer_seat"]:
        raise ConfigError(f"{where}: 'reviewer_seat' (or seats.reviewer.login) is required")

    adj = {k: v for k, v in (loop.get("adjudicator") or {}).items()}
    if adj and not adj.get("route") and set(adj) <= {"profile"}:
        # A route-less adjudicator carries no configuration at all — it is exactly what the
        # normalized form writes. Dropping it is what lets our own output be read back; anything
        # with more than a profile and still no route is a real mistake and raises below.
        adj = {}
    if adj and not adj.get("route"):
        raise ConfigError(f"{where}: adjudicator.route is required when adjudicator is set")
    if adj:
        adj.setdefault("profile", "default")
    loop["adjudicator"] = adj

    loop["observer"] = blocks.normalize_observer(loop.get("observer"))

    loop["cap"] = _as_int(loop["cap"], "cap", where)
    if loop["cap"] < 2:
        raise ConfigError(f"{where}: 'cap' is the number of verdicts allowed; must be >= 2")

    for key in ("review_only_cap", "review_only_daily"):
        loop[key] = _check_review_only_limit(loop.get(key), key, where)
    loop["ci_fix_cap"] = check_ci_fix_cap(loop.get("ci_fix_cap"), where)

    loop["tokens"] = {k: str(v) for k, v in (loop.get("tokens") or {}).items()}
    for login, token_path in loop["tokens"].items():
        # A relative path would resolve from whatever the current directory is at use time.
        problem = relative_path_problem(token_path)
        if problem:
            raise ConfigError(f"{where}: tokens.{login}: {problem}")
    loop["apps"] = blocks.check_apps(loop.get("apps"), loop["tokens"], where)
    # The reader is named, never inferred: taking "the first token" would make whichever seat
    # happens to be listed first the account every gate reads GitHub as.
    loop["read_token"] = str(loop.get("read_token") or "").strip()
    if not loop["read_token"]:
        raise ConfigError(f"{where}: 'read_token' is not set, and the reader is never inferred "
                          "from 'tokens' — add \"read_token\": \"<login>\" naming the reader's "
                          "own account, with its own entry in 'tokens': `hermes dk set "
                          f"--loop {loop.get('id') or '<id>'} --read-token LOGIN --token "
                          f"LOGIN=/abs/path/to/pat` writes both; {FOUR_IDENTITY_RULE}")
    adjudicator_seat = blocks._adjudicator_seat(raw_seats.get("adjudicator"), loop, where)
    if adjudicator_seat:
        seats["adjudicator"] = adjudicator_seat
    loop["triage"] = blocks.normalize_triage(loop.get("triage"), loop, where)
    triage_seat = blocks._triage_seat(raw_seats.get("triage"), where)
    if triage_seat:
        seats["triage"] = triage_seat
    loop["shadow"] = blocks.normalize_shadow(loop.get("shadow"), where)
    shadow_seat = blocks._triage_seat(raw_seats.get("shadow"), where, "shadow")
    if shadow_seat:
        seats["shadow"] = shadow_seat
    loop["roots"] = [str(p) for p in (loop.get("roots") or [])]
    for root in loop["roots"]:
        reason = blocks.dangerous_root(root)
        if reason:
            raise ConfigError(f"{where}: root {root!r} is {reason}; cleanup deletes PR-named "
                              f"children of every root, so a root must be a dedicated directory")

    # `or 1` here would swallow a literal 0 into "serialized", which is the worst kind of silent
    # correction: the operator asked for something invalid and got a loop that looks configured.
    raw_capacity = loop.get("concurrency")
    if raw_capacity is None or raw_capacity == "":
        raw_capacity = 1
    loop["concurrency"] = _as_int(raw_capacity, "concurrency", where)
    if loop["concurrency"] < 1:
        raise ConfigError(f"{where}: 'concurrency' must be >= 1 (1 = serialized)")

    for seat in SEAT_KEYS:
        raw = seats[seat].get("concurrency")
        if raw is None or raw == "":
            continue
        seats[seat]["concurrency"] = _as_int(raw, f"seats.{seat}.concurrency", where)
        if seats[seat]["concurrency"] < 1:
            raise ConfigError(f"{where}: seats.{seat}.concurrency must be >= 1 (1 = serialized)")

    loop["operator_agents"] = check_operator_agents(loop.get("operator_agents"), loop, where)

    loop["turn_budget_s"] = _check_budget(
        DEFAULT_TURN_BUDGET_S if loop.get("turn_budget_s") in (None, "") else loop["turn_budget_s"],
        "'turn_budget_s'", where)
    for seat in SEAT_KEYS:
        if seats[seat].get("turn_budget_s") not in (None, ""):
            seats[seat]["turn_budget_s"] = _check_budget(
                seats[seat]["turn_budget_s"], f"seats.{seat}.turn_budget_s", where)
        if seats[seat].get("daily_turns") not in (None, ""):
            seats[seat]["daily_turns"] = _check_daily_turns(
                seats[seat]["daily_turns"], f"seats.{seat}.daily_turns", where)
        if seats[seat].get("daily_tokens") not in (None, ""):
            seats[seat]["daily_tokens"] = _check_daily_tokens(
                seats[seat]["daily_tokens"], f"seats.{seat}.daily_tokens", where)
        if seats[seat].get("max_steps") not in (None, ""):
            seats[seat]["max_steps"] = _check_max_steps(
                seats[seat]["max_steps"], f"seats.{seat}.max_steps", where)

    if not loop.get("clone") and not isolated_runtime_configured():
        # Above one run at once, isolation is not a preference: with no clone to isolate from and
        # no isolated worker to launch the turns, two runs would share whatever checkout they
        # find, which is the wrong-verdict bug. Check the effective value per seat, so a
        # seat-level 2 is caught even when the loop default is 1.
        for seat in SEAT_KEYS:
            if seat_concurrency(loop, seat) > 1:
                raise ConfigError(
                    f"{where}: seats.{seat}.concurrency > 1 requires 'clone' "
                    f"(runs must be isolated), or the isolated worker's runtime file "
                    f"{host_path('runtime')} (`hermes dk setup` writes it)")

    loop["host"] = webhook_host(loop.get("host"))
    if not loop.get("state_dir"):
        loop["state_dir"] = str(default_state_dir(loop["id"]))
    return loop


def load_file(path: pathlib.Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except Exception as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        return normalize(raw, path)
    except ConfigError:
        raise
    except (TypeError, ValueError, OverflowError, AttributeError, KeyError) as exc:
        # A hand-edited value of the wrong shape is a refusal with a reason, never a traceback
        # out of whichever verb happened to load it.
        raise ConfigError(f"{path}: malformed loop file ({type(exc).__name__}: {exc})") from exc


def _as_int(value, key: str, where: str) -> int:
    """An integer setting, or a ConfigError naming it (``int("many")`` must not escape)."""
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
        # int(3.5) would quietly become 3: a hand-edited fraction is a mistake, not a setting.
        raise ConfigError(f"{where}: {key!r} must be a whole number, got {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):     # int(float("inf")): JSON allows Infinity
        raise ConfigError(f"{where}: {key!r} must be a whole number, got {value!r}") from None


def load_id_for_reader_repair(loop_id: str, reader: str) -> dict | None:
    """The loop ``set --read-token`` may repair: one whose *only* defect is a missing reader.

    Normalized as if ``reader`` were set (every other rule still applies — any other defect
    raises), then handed back with ``read_token`` empty so the caller records and validates the
    change like any other. ``None`` when the file does have a reader (nothing to repair here).
    """
    path = config_dir() / f"{loop_id}.json"
    if not loop_id or pathlib.Path(loop_id).name != loop_id or path.is_symlink() or not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return None
    if not isinstance(raw, dict) or str(raw.get("read_token") or "").strip() or not reader:
        return None
    try:
        loop = normalize({**raw, "read_token": reader}, path)
    except ConfigError:
        raise
    except (TypeError, ValueError, AttributeError, KeyError) as exc:
        raise ConfigError(f"{path}: malformed loop file ({type(exc).__name__}: {exc})") from exc
    if loop["id"] != loop_id:
        raise ConfigError(f"{path}: loop ID does not match filename")
    loop["read_token"] = ""
    return loop


def load_id(loop_id: str) -> dict:
    if not loop_id or pathlib.Path(loop_id).name != loop_id or loop_id in ('.', '..'):
        raise ConfigError('loop ID must name one config file')
    path = config_dir() / f"{loop_id}.json"
    if path.is_symlink():
        raise ConfigError(f"{path}: symlinked loop configs are not allowed")
    if not path.is_file():
        raise ConfigError(f"no loop config named {loop_id!r} in {config_dir()}")
    loop = load_file(path)
    if loop['id'] != loop_id:
        raise ConfigError(f"{path}: loop ID does not match filename")
    return loop


def all_loops() -> list[dict]:
    """Every readable loop, sorted by id. A broken file raises — silently skipping a repo
    would mean silently not driving it, which is the failure mode this tool exists to kill."""
    directory = config_dir()
    if not directory.exists():
        return []
    return [load_id(p.stem) for p in sorted(directory.glob("*.json"))]


def readable_loops() -> tuple[list[dict], list[tuple[str, str]]]:
    """Every loop file that loads, and ``(loop id, reason)`` for each that does not.

    For the callers that must keep working past one bad file — the read-only listings, the cron
    watchdog's sweep of every loop. Callers that act on a whole set of loops keep ``all_loops``'s
    all-or-nothing refusal.
    """
    directory = config_dir()
    if not directory.exists():
        return [], []
    loops, skipped = [], []
    for path in sorted(directory.glob("*.json")):
        try:
            loops.append(load_id(path.stem))
        except ConfigError as exc:
            skipped.append((path.stem, str(exc)))
    return loops, skipped


def loop_ids_for_repo(full_name: str, exclude: str | None = None) -> list[str]:
    """Ids of every config file naming ``full_name``, read raw so a file the loader refuses still
    counts (it may own the repo). ``exclude`` leaves one id out."""
    want = str(full_name or "").strip().lower()
    directory = config_dir()
    if not want or not directory.exists():
        return []
    ids = []
    for path in sorted(directory.glob("*.json")):
        if path.stem == exclude:
            continue
        try:
            raw_repo = str(json.loads(path.read_text()).get("repo") or "").strip().lower()
        except Exception:
            continue
        if raw_repo == want:
            ids.append(path.stem)
    return ids


def loop_for_repo(full_name: str, warn=None) -> dict | None:
    """The one loop that owns ``full_name``, loading each file on its own — for the wake path.

    ``by_repo`` is all-or-nothing, which is right for the verbs that act on every loop but wrong
    for a gate: one hand-edited sibling file the loader refuses must not stop a healthy loop's
    events. A file that will not load is skipped (``warn`` gets one line) — unless it might be
    this repo's own: when its raw ``repo`` names this repo, or cannot be read at all, the
    ownership question has no safe answer and this raises, exactly as ``by_repo`` would.
    """
    want = str(full_name or "").lower()
    directory = config_dir()
    if not directory.exists():
        return None
    matches = []
    for path in sorted(directory.glob("*.json")):
        try:
            loop = load_id(path.stem)
        except ConfigError as exc:
            try:
                raw_repo = str(json.loads(path.read_text()).get("repo") or "").strip().lower()
            except Exception:
                raw_repo = None
            if raw_repo is None or not raw_repo or raw_repo == want:
                raise ConfigError(f"{exc} (it may own {want or 'this repository'}; "
                                  "unattended writes denied until it loads)") from exc
            if warn:
                warn(f"skipping {path.name} (loop for {raw_repo}): {exc}")
            continue
        if loop["repo"] == want:
            matches.append(loop)
    if len(matches) > 1:
        raise ConfigError(f"duplicate loop configs for {want}: unattended writes denied")
    return matches[0] if matches else None


def by_repo(full_name: str) -> dict | None:
    want = str(full_name or "").lower()
    matches = [loop for loop in all_loops() if loop['repo'] == want]
    if len(matches) > 1:
        raise ConfigError(f"duplicate loop configs for {want}: unattended writes denied")
    return matches[0] if matches else None


def state_dir(loop: dict) -> pathlib.Path:
    """The loop's state directory: the one root for every write under it (ledger-adjacent files,
    audits, route intent, artifacts, turn directories). Under the test guard, never inside the
    real Hermes home."""
    from .host_guards import guard_real_home   # here, not module level: one binding (#676)
    return guard_real_home(_path(loop["state_dir"]))


def artifacts_dir(loop: dict, number: int) -> pathlib.Path:
    return state_dir(loop) / "artifacts" / str(number)


def clone_path(loop: dict) -> pathlib.Path | None:
    return _path(loop["clone"]) if loop.get("clone") else None


def fix_ci(loop: dict) -> bool:
    """Whether a failed required check on a fixer's PR becomes a fixer turn (#306). Off unless
    set, and it needs unattended fixer pushes too, which it never turns on."""
    return loop.get("fix_ci") is True and unattended_fixer_push_enabled(loop)


def fixer_scope_lock(loop: dict) -> bool:
    """Whether a fix round's push is held when it changes files outside its findings (#482).
    Off unless set. It acts only where the broker pushes, so it needs unattended fixer pushes."""
    return loop.get("fixer_scope_lock") is True


def review_only_update(loop: dict) -> bool:
    """Whether the host may push a clean merge of the base into a review-only PR. Off unless set."""
    return loop.get("review_only_update") is True


MERGE_CHECK_MODES = ("auto", "on", "off")
INGRESS_MODES = ("repo-hooks", "app-webhook")


def app_ingress(loop: dict) -> bool:
    """Whether this loop's events arrive through a GitHub App's own webhook (#1239), not repo
    hooks: then it has no repo hooks, and the App's host-level route (``app_route``) wakes it."""
    return loop.get("ingress") == "app-webhook" and bool(loop.get("ingress_app"))


def app_route(loop: dict) -> str:
    """The host-level route an App's webhook delivers to: one per App, shared by every loop whose
    events that App delivers (``dk-app-<slug>``). ``""`` for a repo-hooks loop."""
    if not app_ingress(loop):
        return ""
    return "dk-app-" + str(loop["ingress_app"]).lower().removesuffix("[bot]")


def merge_check_on(loop: dict) -> bool:
    """Whether the host posts the ``diaktoros/review`` merge check (#1163 M2b): ``on``, or ``auto``
    with a GitHub App reviewer (whose approvals GitHub never counts, so the check is the block). A
    PAT loop opts in with ``on`` and gets a commit status (which branch protection cannot pin)."""
    mode = loop.get("merge_check") or "auto"
    return mode == "on" or (mode == "auto" and reviewer_is_app(loop))


def review_after_ci(loop: dict) -> bool:
    """Whether a review waits for the head's checks to finish (#241). Off unless set."""
    return loop.get("review_after_ci") is True
