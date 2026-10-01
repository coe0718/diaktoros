"""Read-only preflight — *can this installation run the loop at all?*

``init`` writes a loop config, three routes and (optionally) two GitHub hooks and a cron job.
Every one of those can be syntactically perfect while the installation still cannot run: the
reviewer's profile does not exist, the token file named for the fixer went away in a key
rotation, the route in the gateway registry wakes a *different* profile than the loop config
says, the repo hook points at the operator's previous gateway, or the cron shim is still pinned
to the plugin directory a previous upgrade left behind. A loop that looks armed and cannot wake
a seat — or cannot post a verdict — is the failure this plugin exists to make loud, so the
preflight answers it before anyone arms anything:

    hermes review-loop doctor --loop attest

One line per check, in one of these states:

* ``verified`` — checked, and correct;
* ``absent`` — the thing is not there (a missing profile, token file, route, hook, job, script);
* ``mismatch`` — present, but not what this loop needs (a route waking the wrong profile, a hook
  pointing at another gateway, a shim pinned to a stale plugin path, a world-readable PAT);
* ``unknown`` — could not be decided *from here* (a hooks read the token was not allowed to make,
  a probe skipped with ``--offline``);
* ``skipped`` — not checked because another line already fails for the same cause
  (``extras:<seat>`` while ``model:<seat>`` is absent): neither a pass nor a second warning.

``unknown`` is never folded into ``absent``. "The API refused to tell me" and "there are no
hooks" are different claims, and printing the second one when the first is true sends the
operator hunting for a hook that exists. Failures (``absent``/``mismatch``) exit 1 so a
preflight can gate an install; ``--strict`` makes ``unknown`` a failure too.

Two things this deliberately never does:

* **It writes nothing** — no config, no route registry, no state, no GitHub hook. A preflight
  that repairs what it checks cannot be run against a live install to find out what is wrong.
* **It never fires a route.** A synthetic POST at a seat's route is a real agent run with a real
  budget, so the network side here is a TCP connect (is something listening at the gateway?) and,
  when the token is allowed to, a read of the repo's hooks. There is no test-fire mode on
  purpose: the way to test a route is to hand GitHub a real event.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shlex
import socket
import subprocess
import time
from datetime import datetime
from urllib.parse import urlsplit

from . import config, gh, observer, route_intent, routes

VERIFIED = "verified"
ABSENT = "absent"
MISMATCH = "mismatch"
UNKNOWN = "unknown"
# Not checked because another line already fails for the same cause (``extras:<seat>`` when
# ``model:<seat>`` is ❌): neither a pass nor a second warning.
SKIPPED = "skipped"
FAILURES = (ABSENT, MISMATCH)
MARKS = {VERIFIED: "✅", ABSENT: "❌", MISMATCH: "❌", UNKNOWN: "⚠️", SKIPPED: "➖"}

# What `init` writes, mirrored here because ``cli`` imports this module (so this module cannot
# import ``cli``) and `tests/run_tests.py` asserts the two spellings agree — a preflight that
# looks for a filename nothing writes would report a healthy install as broken.
SHIM_NAME = "review-loop-watchdog.py"
PLUGIN_SCRIPTS = ("watchdog.py", "gate_reviewer.py", "gate_fixer.py",
                  "gate_adjudicator.py", "cleanup.py")
GATE_SCRIPT = {"reviewer": "gate_reviewer.py", "fixer": "gate_fixer.py"}
GATE_EVENT = {"reviewer": "pull_request", "fixer": "pull_request_review"}


class Check:
    """One line of the preflight: what was asked, what was found, and how to fix it."""

    def __init__(self, name: str, status: str, detail: str, fix: str = "",
                 paused: bool = False) -> None:
        self.name = name
        self.status = status
        self.detail = detail
        self.fix = fix
        # A correct hook that is not armed yet. Not a failure: init creates hooks paused and the
        # documented order is doctor → selftest → arm, so a fresh install must be able to pass.
        self.paused = paused

    @property
    def failed(self) -> bool:
        return self.status in FAILURES


def plugin_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1]


def scripts_dir() -> pathlib.Path:
    """Where the plugin's own gate/watchdog/cleanup scripts live — read at call time, so a test
    can point the check at an empty directory without touching the installed plugin."""
    return plugin_root() / "scripts"


def profile_dir(name: str) -> pathlib.Path:
    """A seat profile's home: the root itself for ``default``, else ``profiles/<name>``.

    That is the layout the gateway uses, and it is also where the gates look for a seat's
    ``.env`` (the start-ping reads ``<home>/profiles/<profile>/.env``), so "does this profile
    exist" is answerable offline and without asking the gateway anything.
    """
    if not name or name == "default":
        return config.home()
    return config.home() / "profiles" / name


SHARED_JOB_NAME = "review loop watchdog"  # one shared job runs the shim (#60)


def shim_path() -> pathlib.Path:
    return config.home() / "scripts" / SHIM_NAME


def cron_store() -> pathlib.Path:
    """The scheduler's job store. Its own file, so the preflight needs no scheduler running."""
    return config.home() / "cron" / "jobs.json"


def watchdog_job_name(loop: dict) -> str:
    """The shared job name — one job sweeps every loop (#60). ``loop`` is accepted for callers."""
    return SHARED_JOB_NAME


def cron_fix(loop: dict) -> str:
    """The scheduler's own command for the watchdog job. Never ``init``: it refuses a loop that
    already exists, and the job is the scheduler's to create."""
    return (f"`hermes cron create 15m --name \"{watchdog_job_name(loop)}\" --no-agent "
            f"--script {SHIM_NAME} --deliver local`")


def cron_replace_fix(loop: dict, job_ids) -> str:
    """For a watchdog job that exists but cannot run as it should: remove it (every one, by its
    exact id), then create it. ``hermes cron create`` only appends — it never replaces a job of
    the same name — so printing a bare create here would leave the broken job answering beside
    a duplicate that fires the same shim."""
    removes = ", then ".join(f"`hermes cron remove {job_id}`" for job_id in job_ids)
    return f"{removes}, then {cron_fix(loop)}"


def shim_fix(loop: dict) -> str:
    return (f"`hermes review-loop apply --loop {loop['id']} --watchdog-shim` rewrites it from the "
            "plugin (the scheduled job runs it by name)")


def hooks_fix(loop: dict, what: str) -> str:
    """``apply --hooks`` reconciles this loop's two repo hooks with its routes; ``init --hooks``
    refuses an existing loop."""
    from . import cli       # the one wording of what a hook write needs (#106)
    return (f"`hermes review-loop apply --loop {loop['id']} --hooks --admin-token <login>` {what} "
            f"({cli.hook_write_need(loop, '<login>')})")


def _env_keys(path: pathlib.Path) -> set[str]:
    """Keys with nonempty values in a profile env; never return or report the values."""
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return set()
    keys = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.strip() and not value.strip().startswith("#"):
            keys.add(key.strip())
    return keys


def _nearest_dir(path: pathlib.Path) -> pathlib.Path | None:
    """The deepest existing directory at or above ``path`` — what a write would land in."""
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            return candidate
    return None


# -- the checks ------------------------------------------------------------------


def check_config(loop: dict) -> Check:
    path = config.config_dir() / f"{loop['id']}.json"
    if not path.exists():
        return Check("config", ABSENT, f"no loop config at {path}",
                     "write one with `hermes review-loop init`: the gates select a loop by the "
                     "payload's repository, so a loop nobody can load drives nothing")
    try:
        json.loads(path.read_text())
    except Exception as exc:
        return Check("config", MISMATCH, f"{path} is not readable JSON ({exc})",
                     "repair the file (or re-run init): every gate reads it on every event")
    return Check("config", VERIFIED,
                 f"{path} (repo {loop['repo']}, cap {loop['cap']}, base {loop['base']})")


def check_turn_budget(loop: dict) -> Check:
    """The wall clock each isolated seat turn gets (#49), and every clock that judges it.

    A turn runs from launch to end for up to ``config.worst_turn_s``: the host dependency
    prefetch (#51), the budget, the sandbox kill grace, and the broker drain that lets an
    in-flight write finish (#98). Every age threshold the watchdog applies to a seat follows
    *that seat's* whole turn by construction: its stall grace (``grace_min``, raised to the
    turn — ``config.stall_grace_s``), its seat-lock TTL (``ttl_min``, raised the same way —
    ``config.seat_ttl_s``), the "that run died" report at twice the TTL, and an
    ``adjudicating`` breach marker's stall clock (``config.adjudicating_stall_s``). One seat's
    long budget never lengthens another's. So there is nothing to warn about: doctor prints
    each figure, and names every one the turn raised past the operator's setting.
    """
    seats = ["reviewer", "fixer"] + (["adjudicator"] if (loop.get("adjudicator") or {}).get("route")
                                     else [])
    budgets = {seat: config.turn_budget(loop, seat) for seat in seats}
    detail = " · ".join(f"{seat} {value}s" for seat, value in budgets.items())
    parts = config.turn_parts(loop)
    worst = config.worst_turn_s(loop)
    whole = (f"up to {worst}s launch to end ({parts['prefetch']}s dependency prefetch + "
             f"{parts['budget']}s budget + {parts['grace']}s kill grace + {parts['drain']}s "
             "broker drain)")
    grace_min = int(loop.get("grace_min") or config.DEFAULTS["grace_min"])
    ttl_min = int(loop.get("ttl_min") or config.DEFAULTS["ttl_min"])

    def minutes(seconds: int) -> int:
        return -(-seconds // 60)

    def per_seat(name: str, setting: int, clock) -> str:
        bits = []
        for seat in seats:
            value = minutes(clock(seat))
            bits.append(f"{seat} {value}m" + ("" if value == setting else
                                               " (raised to fit its turn)"))
        return f"{name} " + " · ".join(bits)

    stall_seats = [seat for seat in seats if seat != "adjudicator"]
    stall = "stall grace " + " · ".join(
        f"{seat} {minutes(config.stall_grace_s(loop, seat))}m"
        + ("" if minutes(config.stall_grace_s(loop, seat)) == grace_min
           else " (raised to fit its turn)") for seat in stall_seats)
    lock = (per_seat("seat lock TTL", ttl_min, lambda seat: config.seat_ttl_s(loop, seat=seat))
            + "; 'that run died' after twice that")
    text = (f"{detail} per isolated turn (sandbox killed past it); {whole} — grace_min "
            f"{grace_min}m, ttl_min {ttl_min}m; {stall}; {lock}")
    if "adjudicator" in seats:
        # The breach marker's stall clocks (#98): a ruling in flight is never a stall; one
        # claimed with no live run is, only after the adjudicator's whole turn.
        marker = int(loop.get("marker_grace_min") or config.DEFAULTS["marker_grace_min"])
        text += (f"; breach marker: awaiting-adjudication stalls after {marker}m; adjudicating "
                 "only with no live ruling run, after "
                 f"{minutes(config.adjudicating_stall_s(loop))}m")
    return Check("turn-budget", VERIFIED, text)


def check_watchdog_last_run(loop: dict) -> Check:
    """Has the watchdog actually run recently? (#67)

    ``check_cron_job`` proves the job is *configured*; this proves it *runs*. The watchdog
    stamps ``last_run`` in the loop's ``watchdog.json`` on every sweep, and the job's own
    ``schedule`` gives the interval it should fire at. A ``last_run`` older than **2× the
    schedule** means at least one scheduled run did not happen — the shim is broken, the
    scheduler is down, or the job was paused — and stalls, queue drains and route self-heal
    are all silently skipped while the loop keeps looking armed.

    A missing or unparseable ``last_run``, or a schedule this preflight cannot read, is
    ``unknown`` rather than a failure: nothing here writes state, so a loop whose watchdog
    simply has not fired yet is not proof of a fault. Only a proven-stale stamp fails.
    """
    import time as _time
    from . import state as state_mod
    from .util import epoch
    st = state_mod.state_for(loop)
    raw = st.watch().get("last_run")
    if not raw:
        return Check("watchdog:run", UNKNOWN,
                     "no last_run recorded in watchdog.json — the watchdog may not have "
                     "run yet (or its state is unreadable)",
                     f"run `hermes review-loop watchdog --loop {loop['id']}` once, or wait "
                     "for the next scheduled run; if it never stamps last_run, the shim or "
                     "the scheduler is broken (see cron:shim / cron:job)")
    last = epoch(raw)
    if not last:
        return Check("watchdog:run", UNKNOWN,
                     f"last_run in watchdog.json is not a parseable timestamp ({raw!r})",
                     "check watchdog.json — a corrupt stamp means the watchdog state needs "
                     "repair before staleness can be judged")
    schedule_min = _watchdog_schedule_minutes(loop)
    if schedule_min is None:
        return Check("watchdog:run", UNKNOWN,
                     f"last run {raw} ({(_time.time() - last) / 60:.0f}m ago), but the "
                     "schedule interval could not be read from the cron store",
                     "check `hermes cron list` or the cron store; the staleness threshold "
                     "is 2× the schedule interval")
    threshold_s = 2 * schedule_min * 60
    age_s = _time.time() - last
    if age_s > threshold_s:
        return Check("watchdog:run", MISMATCH,
                     f"last run {raw} ({age_s / 60:.0f}m ago) is older than 2× the "
                     f"{schedule_min}m schedule ({threshold_s / 60:.0f}m) — the watchdog "
                     "has stopped running",
                     f"check the cron job (`hermes cron list`), the shim (`{shim_path()}`) "
                     f"and the watchdog log ({st.log}); resume or replace the job with "
                     f"{cron_fix(loop)}")
    return Check("watchdog:run", VERIFIED,
                 f"last run {raw} ({age_s / 60:.0f}m ago, within 2× the {schedule_min}m "
                 f"schedule = {threshold_s / 60:.0f}m)")


def _watchdog_schedule_minutes(loop: dict) -> int | None:
    """The watchdog job's schedule interval in minutes, or None if it cannot be read."""
    path = cron_store()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return None
    wanted = watchdog_job_name(loop)
    job = next((entry for entry in jobs if isinstance(entry, dict)
                and str(entry.get("name") or "").strip() == wanted), None)
    if job is None:
        return None
    schedule = job.get("schedule")
    if isinstance(schedule, dict) and schedule.get("kind") == "interval":
        minutes = schedule.get("minutes")
        if type(minutes) is int and minutes > 0:
            return minutes
    return None


def check_runtime_paths(loop: dict) -> list[Check]:
    """Validate every path the runtime file names. (#67)

    ``review-loop-runtime.json`` names four host paths (``source``, ``venv``, ``runtime``,
    ``rust``) that the isolated worker mounts. A Hermes upgrade that moves the install
    leaves those paths pointing at directories that no longer exist — and the worker fails
    on every turn while doctor says nothing. Each path is checked independently so the
    operator sees exactly which one is wrong; a missing runtime file is reported as such.
    """
    from . import seat_model
    path = config.home() / "review-loop-runtime.json"
    if not path.exists():
        return [Check("runtime:file", ABSENT,
                      f"no runtime file at {path}",
                      f"write {path} with source/venv/runtime/rust paths (see "
                      "`docs/configuration.md`); without it no isolated turn can start")]
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("must be a private (0600) regular file")
        settings = seat_model.load_runtime(path)
    except (OSError, ValueError) as exc:
        return [Check("runtime:file", MISMATCH,
                      f"{path}: {exc}",
                      f"repair {path}; `hermes review-loop selftest` shows each problem")]
    checks: list[Check] = []
    for key in ("source", "venv", "runtime", "rust"):
        raw = settings.get(key)
        if not raw:
            checks.append(Check(f"runtime:{key}", ABSENT,
                                f"no {key} path in the runtime file",
                                f"set {key} in {path} to the correct host path"))
            continue
        p = pathlib.Path(raw).expanduser()
        if not p.exists():
            checks.append(Check(f"runtime:{key}", ABSENT,
                                f"{key} = {raw} does not exist",
                                f"update {key} in {path} to the current path (a Hermes "
                                "upgrade may have moved it)"))
        elif key == "source" and not (p / "run_agent.py").exists():
            checks.append(Check(f"runtime:{key}", MISMATCH,
                                f"{key} = {raw} exists but has no run_agent.py",
                                f"point {key} at the hermes-agent Git checkout"))
        elif key == "venv" and not (p / "bin" / "python").exists():
            checks.append(Check(f"runtime:{key}", MISMATCH,
                                f"{key} = {raw} exists but has no bin/python",
                                f"point {key} at the virtualenv Hermes is installed in"))
        elif key == "rust" and not (p / "bin" / "cargo").exists():
            checks.append(Check(f"runtime:{key}", MISMATCH,
                                f"{key} = {raw} exists but has no bin/cargo",
                                f"point {key} at the Rust toolchain directory"))
        else:
            checks.append(Check(f"runtime:{key}", VERIFIED, f"{key} = {raw}"))
    return checks


def _check_profile(name: str, seat: str) -> Check:
    if not name:
        return Check(f"profile:{seat}", ABSENT, "no profile named for this seat",
                     f"re-run init with --{seat}-profile <an existing profile>")
    path = profile_dir(name)
    if path.is_dir():
        return Check(f"profile:{seat}", VERIFIED, f"{name} → {path}")
    return Check(f"profile:{seat}", ABSENT, f"no profile home at {path}",
                 f"`hermes profile create {name}`, or re-run init with --{seat}-profile pointing "
                 f"at a profile that exists: the run happens as this profile")

def check_profile(loop: dict, seat: str) -> Check:
    return _check_profile(str(loop["seats"][seat].get("profile") or ""), seat)

def check_adjudicator_profile(loop: dict) -> Check:
    return _check_profile(str((loop.get("adjudicator") or {}).get("profile") or "default"),
                          "adjudicator")



def runtime_settings() -> tuple[dict | None, Check | None]:
    """The runtime file's settings for the model checks, or a check explaining why not."""
    from . import seat_model
    path = config.home() / "review-loop-runtime.json"
    if not path.exists():
        return None, None
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("must be a private (0600) regular file")
        return seat_model.load_runtime(path), None
    except (OSError, ValueError) as exc:
        return None, Check("runtime", MISMATCH, f"{path}: {exc}",
                           f"repair {path}; `hermes review-loop selftest` shows each problem")


def check_seat_models(loop: dict) -> list[Check]:
    """Each seat's model as the worker will pick it — profile, provider, model; never the key.

    Read-only: the profile's ``config.yaml`` ``model`` block is read by the Hermes interpreter,
    without resolving (or refreshing) any credential. ``selftest`` resolves the credential and
    makes one tiny completion per seat.
    """
    from . import seat_model
    settings, problem = runtime_settings()
    checks = [problem] if problem else []
    if settings is not None and seat_model.legacy_override(settings) is not None:
        checks.append(Check("runtime:legacy-model", UNKNOWN,
                            "review-loop-runtime.json still sets a top-level model/upstream/"
                            "key_file: a LEGACY fallback used only for a seat whose profile "
                            "cannot be resolved",
                            "drop it once every seat's profile resolves, or move it under "
                            "seats.<seat> as an explicit per-seat override"))
    status_of = {"ok": VERIFIED, "warn": UNKNOWN, "fail": ABSENT}
    for seat in seat_model.seats_for(loop):
        try:
            status, detail, fix = seat_model.describe_seat(loop, seat, settings)
        except Exception as exc:        # e.g. a malformed base_url: undecided, never a crash
            status, detail, fix = ("warn", f"could not describe this seat ({type(exc).__name__}: "
                                           f"{exc})", "run `hermes review-loop selftest`")
        checks.append(Check(f"model:{seat}", status_of[status], detail, fix))
    return checks

# Read-only: ``find_spec`` locates a module without importing (or installing) anything.
# ``-I`` keeps the answer the venv's own — no PYTHONPATH, user site-packages or cwd — which is
# what the sandbox's Hermes resolves against; a dotted name whose parent is missing is "absent".
_FIND_SPEC = ("import importlib.util, sys\n"
              "try:\n    found = importlib.util.find_spec(sys.argv[1]) is not None\n"
              "except (ImportError, ValueError):\n    found = False\n"
              "sys.exit(0 if found else 1)\n")
EXTRA_PROBE_TIMEOUT = 20


def venv_has_module(python: str, module: str) -> bool | None:
    """True/False: can ``python`` import ``module``; ``None`` when it could not say."""
    try:
        process = subprocess.run([python, "-I", "-c", _FIND_SPEC, module],
                                 env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                 stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=EXTRA_PROBE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(process.returncode)


def check_seat_extras(loop: dict) -> list[Check]:
    """#118: each seat's provider's optional Hermes package, importable by the runtime's venv.

    A Claude-subscription (or any Messages-wire) seat needs Hermes's ``anthropic`` extra; an
    install without it looks healthy until the seat's first turn fails at model setup. The venv
    checked is the one the runtime file names — the one the sandbox mounts — and it is asked
    with ``find_spec`` (read-only, no network, bounded). The provider comes from the same
    read-only description as ``model:<seat>``: no credential is resolved. A seat whose provider
    could not be read, or a probe that could not answer, is ``unknown`` — never ``verified``.
    """
    from . import seat_model
    runtime = config.home() / "review-loop-runtime.json"
    settings, problem = runtime_settings()
    venv = str((settings or {}).get("venv") or "")
    probes: dict[str, bool | None] = {}
    checks = []
    for seat in seat_model.seats_for(loop):
        name = f"extras:{seat}"
        if not venv:
            why = f"{runtime} is not usable" if problem else f"no runtime file at {runtime}"
            checks.append(Check(name, UNKNOWN, f"{why}, so the Hermes venv the {seat} turn "
                                "mounts is not known; its provider package was not checked",
                                f"write {runtime} naming source/venv/runtime/rust"))
            continue
        needed = maybe = None
        explain: dict = {}
        try:
            status, _, _, wire = seat_model.describe_seat_wire(loop, seat, settings)
            if wire is not None and status != "fail":
                needed, maybe = seat_model.extras_for(
                    wire["provider"], wire["api_mode"], wire["base_url"],
                    model=wire.get("model") or "", configured=wire.get("configured") or "",
                    facts=wire.get("facts"), entry=wire.get("entry"), hermes=wire.get("hermes"),
                    entry_hint=bool(wire.get("entry_hint")), explain=explain)
        except Exception as exc:        # a malformed URL, a broken description: undecided
            status, wire, reason = "warn", None, f" ({type(exc).__name__}: {exc})"
        else:
            reason = ""
        if status == "fail":
            checks.append(Check(name, SKIPPED, f"skipped: model unresolved (see model:{seat})"))
            continue
        if wire is None:
            checks.append(Check(name, UNKNOWN, f"the {seat} seat's provider could not be read"
                                f"{reason} (see model:{seat}), so whether it needs an optional "
                                "Hermes package is unknown",
                                f"fix model:{seat} first, then re-run doctor"))
            continue
        provider, model = wire["provider"], wire.get("model") or ""
        # The wire Hermes will use when it is decided (a named entry, nous native, ...), not
        # the profile's own spelling.
        mode = explain.get("wire") or wire["api_mode"]
        not_asked = str(wire.get("not_asked") or "")
        if not needed and not maybe:
            if not_asked:       # a "not needed" from doctor's own table is not a pass
                checks.append(Check(name, UNKNOWN, f"{provider} [{mode}] needs no optional Hermes "
                                    f"package by doctor's own table only — Hermes was not asked "
                                    f"({not_asked})", f"fix model:{seat} first, then re-run doctor"))
            else:
                checks.append(Check(name, VERIFIED, f"{provider} [{mode}] needs no optional "
                                    "Hermes package"))
            continue
        python = str(pathlib.Path(venv) / "bin" / "python")

        def probe(extra: str) -> bool | None:
            module = seat_model.HERMES_EXTRAS[extra]["module"]
            if module not in probes:
                probes[module] = venv_has_module(python, module)
            return probes[module]

        missing = [e for e in needed if probe(e) is False]
        undecided = [e for e in needed if probe(e) is None]
        maybe_missing = [e for e in maybe if probe(e) is not True]
        what = ", ".join(f"`{e}` (import {seat_model.HERMES_EXTRAS[e]['module']})"
                         for e in needed + maybe)
        switch = "; ".join(
            f"may need the {e} extra: {provider} can use Anthropic's native wire for {model} "
            f"{explain.get(e, '')}".rstrip()
            for e in maybe_missing)
        because = "; ".join(explain[e] for e in needed if explain.get(e))
        install = lambda extras: " and ".join(f"`hermes pm install --extra {e}`" for e in extras)
        where = (f"it installs into the venv Hermes selects, so if that is not {venv}, install the "
                 f"extra into {venv} or point `venv` in {runtime} at the venv that has it")
        if missing:
            checks.append(Check(
                name, ABSENT,
                f"{provider} [{mode}] needs the Hermes extra {what}"
                + (f" ({because})" if because else "")
                + f", which {python} cannot import; the {seat} turn would fail at model setup"
                + (f" ({switch})" if switch else ""),
                f"{install(missing + maybe_missing)} (Hermes's own command for a missing extra) "
                f"— {where}; then re-run doctor"))
        elif undecided or maybe_missing:
            if switch and not undecided:
                detail = (f"{switch}; {python} cannot import it (install: "
                          f"{install(maybe_missing)} — {where})")
            else:
                detail = (f"{provider} [{mode}] needs the Hermes extra {what}; {python} could not "
                          "answer whether it is importable (missing, not a python, or no answer "
                          "in time)")
            checks.append(Check(name, UNKNOWN, detail, f"check `venv` in {runtime}"))
        else:
            label = "may use" if maybe and not needed else "needs"
            checks.append(Check(name, VERIFIED,
                                f"{provider} [{mode}] {label} {what}: importable by {python}"))
    return checks


def _token_file_facts(path: pathlib.Path) -> str:
    """``path (exists: yes, private: no)`` — metadata only; the file is never opened here."""
    exists = path.exists()
    private = exists and not config.token_file_problem(str(path))
    return (f"{path} (exists: {'yes' if exists else 'no'}, "
            f"private: {'yes' if private else 'no'})")


def check_credential(loop: dict, seat: str) -> Check:
    """The gates use gh.token_path, not the profile's GH_TOKEN environment variable."""
    login = str(loop["seats"][seat].get("login") or "")
    tokens = loop.get("tokens") or {}
    if login:
        path = gh.token_path(loop, login)
        if path and path.is_file() and path.stat().st_size > 0:
            facts = _token_file_facts(path)
            if config.token_file_problem(str(path)):
                return Check(f"credential:{seat}", MISMATCH,
                             f"{login} → {facts}: {config.token_file_problem(str(path))}",
                             f"chmod 600 {path} (and own it): a token file other users can read "
                             "is a shared credential")
            return Check(f"credential:{seat}", VERIFIED,
                         f"{login} → {facts}, nonempty (identity and API access not checked)")
    env = profile_dir(str(loop["seats"][seat].get("profile") or "")) / ".env"
    if "GH_TOKEN" in _env_keys(env):
        return Check(f"credential:{seat}", ABSENT,
                     f"nonempty GH_TOKEN in {env}, but no mapped token file for "
                     f"{login or seat}; a profile environment alone does not provide the "
                     "gate's configured GitHub identity, and the sandboxed seat never sees it",
                     f"map a token file for {login or '<login>'}: re-run init with "
                     f"--token {login or '<login>'}=/path/to/pat, or set {seat}_token_file in the "
                     "plugin settings and run apply; then remove GH_TOKEN from that .env")
    who = login or f"the {seat} seat"
    mapped = gh.token_path(loop, login) if login else None
    if mapped is not None:
        return Check(f"credential:{seat}", ABSENT,
                     f"{login} → {_token_file_facts(mapped)}: missing or empty",
                     f"write the PAT for {login} to {mapped} (chmod 600)")
    # Seats write only through the host broker with a mapped token file; a GH_TOKEN in the
    # profile's .env is never used, so it is not offered as a fix.
    return Check(f"credential:{seat}", ABSENT,
                 f"no token file mapped for {who!r} (a GH_TOKEN in {env} would not be used)",
                 f"re-run init with --token {login or '<login>'}=/path/to/pat, or set "
                 f"{seat}_token_file in the plugin settings and run apply — without one the seat "
                 "cannot push or post a verdict")


def check_adjudicator_identity(loop: dict) -> Check | None:
    """The optional identity a ruling is also posted as; ``None`` when the loop has none.

    Without one, rulings go to the operator feed and the host ledger only — that is a valid
    configuration, not a failure, so nothing is reported. With one, it must be a fourth account:
    its own login and its own token file, never the reader's or a seat's. (Distinct *principals*
    need a network read; the broker verifies those via ``/user`` before every comment.)
    """
    login = config.adjudicator_login(loop)
    if not login:
        return None
    seats = loop.get("seats") or {}
    others = {"read_token": loop.get("read_token"),
              "reviewer": (seats.get("reviewer") or {}).get("login"),
              "fixer": (seats.get("fixer") or {}).get("login")}
    for role, other in others.items():
        if isinstance(other, str) and other and other.casefold() == login.casefold():
            return Check("credential:adjudicator", MISMATCH,
                         f"{login} is also the {role} — a ruling would post as that identity",
                         "set seats.adjudicator.login to its own account, or remove it for "
                         "operator-only rulings")
    path = gh.token_path(loop, login)
    if path is None:
        return Check("credential:adjudicator", ABSENT, f"no tokens entry for {login!r}",
                     f"add --token {login}=/path/to/pat, or remove seats.adjudicator.login for "
                     "operator-only rulings")
    if not path.is_file() or path.stat().st_size == 0:
        return Check("credential:adjudicator", ABSENT,
                     f"{login} → {_token_file_facts(path)}: no nonempty token file",
                     f"write the PAT for {login} (chmod 600)")
    if config.token_file_problem(str(path)):
        return Check("credential:adjudicator", MISMATCH,
                     f"{login} → {_token_file_facts(path)}: "
                     f"{config.token_file_problem(str(path))}",
                     f"chmod 600 {path} (and own it): the ruling identity needs a private "
                     "credential")
    for role, other in others.items():
        theirs = gh.token_path(loop, other) if isinstance(other, str) and other else None
        try:
            shared = theirs is not None and theirs.exists() and path.samefile(theirs)
        except OSError:
            shared = True
        if shared:
            return Check("credential:adjudicator", MISMATCH,
                         f"{login} reads the same token file as the {role}",
                         f"give {login} its own PAT file: a shared file is one account")
    return Check("credential:adjudicator", VERIFIED,
                 f"{login} → its own token file {_token_file_facts(path)}; rulings are also posted as a PR comment "
                 "(principal checked by the broker before each comment)")


def check_token(login: str, raw: str) -> Check:
    """One named credential file. The PAT's *bytes* are never printed, hashed or compared."""
    path = pathlib.Path(str(raw)).expanduser()
    if not path.exists():
        return Check(f"token:{login}", ABSENT, f"no file at {path}",
                     f"write the PAT for {login} to {path} (chmod 600), or re-run init with "
                     f"--token {login}=<a path that exists>")
    if not path.is_file():
        return Check(f"token:{login}", MISMATCH, f"{path} is not a file",
                     f"point --token {login} at a regular file holding the PAT")
    try:
        body = path.read_text()
    except Exception as exc:
        return Check(f"token:{login}", MISMATCH, f"{path} cannot be read ({exc})",
                     f"chmod 600 {path} so the user the gateway runs as can read it")
    if not body.strip():
        return Check(f"token:{login}", MISMATCH, f"{path} is empty",
                     f"write the PAT into {path}: an empty credential reads as an anonymous "
                     f"request, which GitHub answers with 404")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return Check(f"token:{login}", MISMATCH,
                     f"{path} is mode {mode:03o} — readable by group/other users",
                     f"chmod 600 {path}")
    return Check(f"token:{login}", VERIFIED, f"{path} (mode {mode:03o}, non-empty)")


def check_tokens(loop: dict) -> list[Check]:
    tokens = loop.get("tokens") or {}
    if not tokens:
        return [Check("tokens", ABSENT, "no token file is named for any login",
                      "re-run init with --token <login>=/path/to/pat (the gates read GitHub "
                      "through it; without one they go silent)")]
    return [check_token(login, raw) for login, raw in sorted(tokens.items())]


def check_read_token(loop: dict) -> Check:
    name = str(loop.get("read_token") or "")
    if not name:
        return Check("read_token", ABSENT, "no login is named as the reader",
                     "re-run init with --read-token <login> and --token <login>=/path/to/pat")
    if name not in (loop.get("tokens") or {}):
        return Check("read_token", MISMATCH, f"read_token names {name!r}, which has no token file",
                     f"re-run init with --token {name}=/path/to/pat: the gates read every PR "
                     f"state as this login")
    problem = config.reader_problem(loop)
    if problem:
        # The broker refuses every write while the reader is a seat, so a loop in this shape
        # installs, "passes", and then never posts a review.
        return Check("read_token", MISMATCH, f"{problem} — {config.FOUR_IDENTITY_RULE}",
                     config.reader_fix(loop))
    return Check("read_token", VERIFIED, f"{name} (mapped in tokens; its own account and file)")


def _route_entry(data: dict, name: str) -> dict | None:
    entry = data.get(name)
    return entry if isinstance(entry, dict) else None


def check_routes(loop: dict) -> list[Check]:
    """Route/profile/secret correspondence, read from the gateway's own subscription file."""
    path = routes.subs_path()
    if not path.exists():
        return [Check("routes", ABSENT, f"no route registry at {path}", _missing_route_fix(loop))]
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return [Check("routes", MISMATCH, f"{path} is not readable JSON ({exc})",
                      "repair the subscription file: the gateway can route nothing out of it")]
    if not isinstance(data, dict):
        return [Check("routes", MISMATCH, f"{path} must hold a JSON object of routes",
                      "repair the subscription file: the gateway can route nothing out of it")]
    checks = [check_route(loop, data, seat) for seat in ("reviewer", "fixer")]
    adjudicator = check_adjudicator_route(loop, data)
    if adjudicator:
        checks.append(adjudicator)
    feed = check_observer_route(loop, data)
    if feed:
        checks.append(feed)
    return _intent_overlay(loop, data, checks)


REPAIR_FIX = ("the next armed watchdog sweep restores it from the plugin's intent record with the "
              "same secret — or run `hermes review-loop doctor --repair` now; if the change was "
              "intended, make it through `hermes review-loop set/apply/uninstall` instead")


def _intent_overlay(loop: dict, data: dict, checks: list[Check]) -> list[Check]:
    """Compare the live registry with the plugin's own record of its routes (issue #1).

    Read-only, like every check here. A route another registry writer changed can still look
    well-formed (a fresh secret, say) — only the record knows it no longer matches GitHub's hook.
    With no record yet (an install that predates it) the route checks stand as they are.
    """
    try:
        intent = route_intent.load(loop)
    except route_intent.IntentError as exc:
        return checks + [Check("route-intent", MISMATCH, str(exc),
                               "restore the file, or re-run `hermes review-loop apply` so the "
                               "plugin records its routes again")]
    if intent is None:
        return checks
    drifted = route_intent.drift(loop, data, intent)
    by_name = {check.name: check for check in checks}
    for name in sorted(set(intent) & set(route_intent.routes_of(loop).values())):
        check = by_name.get(f"route:{name}")
        fields = drifted.get(name)
        if check is None:
            if fields:
                checks.append(Check(f"route:{name}", MISMATCH,
                                    "differs from the plugin's intent record: " + ", ".join(fields),
                                    REPAIR_FIX))
            continue
        if not fields:
            if check.status == VERIFIED:
                check.detail += " · matches intent record"
            continue
        what = ("erased by another registry writer" if fields == ["missing"]
                else "differs from the plugin's intent record: " + ", ".join(fields))
        if check.status == VERIFIED:
            check.status, check.detail = MISMATCH, what
        else:
            check.detail += f" ({what})"
        live = data.get(name)
        if isinstance(live, dict) and not route_intent.owned(live):
            # Repair reports a route something else now runs as a conflict and never overwrites
            # it, so it can only be the second step.
            check.fix = (f"remove or rename that entry (it now runs {live.get('script')!r}, not a "
                         "review-loop gate, so repair and apply leave it alone), then "
                         f"`hermes review-loop doctor --loop {loop['id']} --repair`")
        # The observer check already chose between repair and a rebuild: repair only restores
        # the record, which is no fix when the record itself breaks the feed's contract.
        elif not (check.fix and name == (loop.get("observer") or {}).get("route")):
            check.fix = REPAIR_FIX
    return checks


def _missing_route_fix(loop: dict) -> str:
    """``init`` refuses an existing loop, so a lost route is written back by ``apply``. (With an
    intent record, ``_intent_overlay`` replaces this with ``doctor --repair``, same secret.)"""
    from . import gate_shims
    return gate_shims.recreate_fix(loop)


def _apply_fix(loop: dict, what: str) -> str:
    """``apply`` rewrites this loop's own routes from its config (secret kept): the remedy for
    every route field it reconciles. Never ``init``, which refuses an existing loop."""
    return f"run `hermes review-loop apply --loop {loop['id']}` to {what} (its secret is kept)"


def _secret_fix(loop: dict) -> str:
    """A route with no secret cannot be rewritten in place: apply keeps the secret it finds, and
    a new one would not match the repo hook. Recreating it mints one and re-keys the hook."""
    return (f"remove that entry (a route without a secret can never verify a signature), then "
            f"{_missing_route_fix(loop)}")


def _host_fixes(loop: dict) -> tuple[str, str]:
    """(invalid origin, no origin): both name the loop's origin with ``set``, then ``apply``."""
    lid = loop["id"]
    both = (f"`hermes review-loop set --loop {lid} --host https://your-gateway.example`, then "
            f"`hermes review-loop apply --loop {lid}` so the routes carry it")
    return both, both


def _prompt_fix(loop: dict, name: str) -> str:
    """The remedy for a route whose prompt no longer proves it is ours: the same one apply prints
    (``gate_shims.divergence``) — repair from the record when it holds the real prompt, else
    move the entry aside and recreate it."""
    from . import gate_shims
    found = gate_shims.divergence(loop, contract=True).get(name)
    return found[1] if found else REPAIR_FIX


def check_route(loop: dict, data: dict, seat: str) -> Check:
    name = str(loop["seats"][seat].get("route") or "")
    profile = str(loop["seats"][seat].get("profile") or "")
    if not name:
        return Check(f"route:{seat}", ABSENT, "no route named for this seat",
                     f"name the route as seats.{seat}.route in the loop config, then "
                     f"`hermes review-loop apply --loop {loop['id']} --recreate-routes`")
    entry = _route_entry(data, name)
    if entry is None:
        return Check(f"route:{name}", ABSENT, f"not in {routes.subs_path().name}",
                     _missing_route_fix(loop))
    if routes.route_profile(entry) != profile:
        return Check(f"route:{name}", MISMATCH,
                     f"wakes profile {entry.get('profile')!r}, but seats.{seat}.profile is "
                     f"{profile!r} — the wake would run the wrong agent",
                     _apply_fix(loop, f"rebind it to {profile or 'the configured profile'}"))
    if entry.get("deliver_only"):
        return Check(f"route:{name}", MISMATCH,
                     "deliver_only is set — the gateway delivers the rendered prompt and runs no "
                     "agent, so this seat is never woken",
                     f"run `hermes review-loop apply --loop {loop['id']}` to rewrite it from the "
                     "loop config (its secret is kept)")
    if entry.get("enabled", True) is False:
        return Check(f"route:{name}", MISMATCH,
                     "disabled in the registry (enabled: false) — the gateway answers 403 to "
                     "every event, so this seat is never woken",
                     f"run `hermes review-loop apply --loop {loop['id']}` to re-enable it (its "
                     "secret is kept)")
    if not str(entry.get("secret") or ""):
        return Check(f"route:{name}", ABSENT, "registered without a secret",
                     _secret_fix(loop))
    if not str(entry.get("prompt") or ""):
        return Check(f"route:{name}", ABSENT, "registered without a prompt",
                     _prompt_fix(loop, name))
    if entry.get("prompt") != route_intent.ROUTE_PROMPT[seat]:
        return Check(f"route:{name}", MISMATCH,
                     f"does not carry the {seat} gate's prompt — the wake would run an agent on "
                     "another protocol, and nothing proves the route is this plugin's",
                     _prompt_fix(loop, name))
    script = str(entry.get("script") or "")
    if script != GATE_SCRIPT[seat]:
        return Check(f"route:{name}", MISMATCH,
                     f"runs gate script {script or '(none)'!r}, expected "
                     f"{GATE_SCRIPT[seat]!r}",
                     _prompt_fix(loop, name))
    events = entry.get("events")
    if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
        return Check(f"route:{name}", MISMATCH, "events must be a list of event names",
                     _apply_fix(loop, f"rewrite it with the {seat} gate's event"))
    if GATE_EVENT[seat] not in events:
        return Check(f"route:{name}", MISMATCH,
                     f"events {events or '(none)'} do not include {GATE_EVENT[seat]!r}",
                     _apply_fix(loop, f"rewrite it with {GATE_EVENT[seat]!r}: the gateway only "
                                "routes the events a route subscribes to"))
    host = str(loop.get("host") or "")
    try:
        url = routes.url_for(name, host or None)
    except config.ConfigError:
        return Check(f"route:{name}", MISMATCH, "invalid webhook host (URL withheld)",
                     _host_fixes(loop)[0])
    if not url:
        return Check(f"route:{name}", ABSENT, "no webhook URL (neither the loop nor the route "
                                              "names a gateway origin)",
                     _host_fixes(loop)[1])
    stored = str(entry.get("host") or "").removesuffix("/")
    if host and stored and stored != host:
        return Check(f"route:{name}", MISMATCH,
                     "registered gateway origin differs from the loop's configured origin (URLs withheld)",
                     _apply_fix(loop, "rewrite it at the loop's origin"))
    return Check(f"route:{name}", VERIFIED,
                 f"{profile} · {GATE_EVENT[seat]} · [webhook URL redacted]")


def check_adjudicator_route(loop: dict, data: dict) -> Check | None:
    """The escalation route must use the post-#21 adjudicator gate."""
    adjudicator = loop.get("adjudicator") or {}
    name = str(adjudicator.get("route") or "")
    if not name:
        return None
    profile = str(adjudicator.get("profile") or "default")
    entry = _route_entry(data, name)
    if entry is None:
        return Check(f"route:{name}", ABSENT, f"not in {routes.subs_path().name}",
                     _missing_route_fix(loop) + " — the breach marker is the only record of an "
                     "escalation nobody is woken for")
    if routes.route_profile(entry) != profile:
        return Check(f"route:{name}", MISMATCH,
                     f"wakes profile {entry.get('profile')!r}, but adjudicator.profile is "
                     f"{profile!r}",
                     _apply_fix(loop, f"rebind it to {profile}: the ruling must not happen as one "
                                "of the two seats that just stalled"))
    if entry.get("deliver_only"):
        return Check(f"route:{name}", MISMATCH,
                     "deliver_only is set — the gateway delivers the rendered prompt and runs no "
                     "agent, so no adjudicator is woken",
                     f"run `hermes review-loop apply --loop {loop['id']}` to rewrite it from the "
                     "loop config (its secret is kept)")
    if entry.get("enabled", True) is False:
        return Check(f"route:{name}", MISMATCH,
                     "disabled in the registry (enabled: false) — the gateway answers 403 to "
                     "every event, so no adjudicator is woken",
                     f"run `hermes review-loop apply --loop {loop['id']}` to re-enable it (its "
                     "secret is kept)")
    if not str(entry.get("secret") or ""):
        return Check(f"route:{name}", ABSENT, "registered without a secret",
                     _secret_fix(loop))
    if not str(entry.get("prompt") or ""):
        return Check(f"route:{name}", ABSENT, "registered without a prompt",
                     _prompt_fix(loop, name))
    if entry.get("prompt") != route_intent.ROUTE_PROMPT["adjudicator"]:
        return Check(f"route:{name}", MISMATCH,
                     "does not carry the adjudicator gate's prompt — the ruling would run on "
                     "another protocol, and nothing proves the route is this plugin's",
                     _prompt_fix(loop, name))
    events = entry.get("events")
    if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
        return Check(f"route:{name}", MISMATCH, "events must be a list of event names",
                     _apply_fix(loop, "rewrite it with the adjudicator gate's event"))
    if "pull_request" not in events:
        return Check(f"route:{name}", MISMATCH,
                     f"events {events or '(none)'} do not include 'pull_request'",
                     _apply_fix(loop, "rewrite it with 'pull_request': breach wakes use it"))
    script = str(entry.get("script") or "")
    if script in config.LEGACY_GATE_SCRIPTS["adjudicator"]:
        # Installed before the dedicated adjudicator gate. `init` refuses an existing loop, so
        # the hint names the command that rewrites this route in place, secret kept.
        return Check(f"route:{name}", MISMATCH,
                     f"runs {script!r} (installed by an older release), expected "
                     "'gate_adjudicator.py'",
                     f"run `hermes review-loop apply --loop {loop['id']}` to rebind it to "
                     "gate_adjudicator.py (its secret is kept)")
    if script != "gate_adjudicator.py":
        return Check(f"route:{name}", MISMATCH,
                     f"runs {script or '(none)'!r}, expected 'gate_adjudicator.py'",
                     _prompt_fix(loop, name))
    if not (scripts_dir() / script).is_file():
        return Check(f"route:{name}", ABSENT, f"gate_adjudicator.py missing from {scripts_dir()}",
                     "reinstall the plugin")
    host = str(loop.get("host") or "").removesuffix("/")
    stored = str(entry.get("host") or "").removesuffix("/")
    if host and stored and stored != host:
        return Check(f"route:{name}", MISMATCH,
                     "registered gateway origin differs from the loop's configured origin (URLs "
                     "withheld)", _apply_fix(loop, "rewrite it at the loop's origin"))
    return Check(f"route:{name}", VERIFIED, f"{profile} · adjudication wake")


def check_observer_route(loop: dict, data: dict) -> Check | None:
    """The observer feed's route: present, serving the profile the loop names, and exactly the
    delivery-only contract the feed checks before every notice (``observer.route_contract``).

    Nothing to check when the loop has no feed. A misconfigured feed has no route to check, but
    it is still a feed that delivers nothing, so it is reported rather than skipped.
    """
    cfg = loop.get("observer") or {}
    if not cfg:
        return None
    if cfg.get("misconfigured"):
        return Check("route:observer", MISMATCH, str(cfg["misconfigured"]),
                     f"`hermes review-loop set --loop {loop['id']} --observer-profile <name>` "
                     "(or --observer-disable): the feed delivers nothing as configured")
    name = str(cfg.get("route") or "")
    profile = config.seat_profile(loop, "observer")
    remedy = observer.route_remedy(loop)
    entry = _route_entry(data, name)
    if entry is None:
        return Check(f"route:{name}", ABSENT, f"not in {routes.subs_path().name}",
                     f"{remedy} writes it again")
    served = routes.route_profile(entry)
    if served is None:
        return Check(f"route:{name}", MISMATCH,
                     f"profile {entry.get('profile')!r} is blank or not a name — the gateway "
                     f"refuses every request for it, and the feed refuses to deliver through it",
                     f"run `hermes review-loop apply --loop {loop['id']}` to rebind it to "
                     f"{profile} (its secret is kept)")
    if served != profile:
        return Check(f"route:{name}", MISMATCH,
                     f"wakes profile {served!r}, but observer.profile is {profile!r} — the "
                     "feed refuses to deliver through it",
                     f"run `hermes review-loop apply --loop {loop['id']}` to rebind it to "
                     f"{profile} (its secret is kept)")
    # The very comparison the feed makes before every notice (routes.target) — with the checks
    # above and below, this is exactly observer._target(): doctor verifies the route if and only
    # if the feed would deliver through it (tests sweep every registry field for that). Drift
    # from the plugin's intent record is the one stricter case, reported by _intent_overlay.
    contract = observer.route_contract(loop)
    wrong = [key for key in routes.contract_mismatch(entry, contract) if key != "profile"]
    if wrong:
        return Check(f"route:{name}", MISMATCH,
                     "does not match the observer delivery-only contract: " + ", ".join(wrong)
                     + " — the feed refuses to deliver through it", f"{remedy} restores it")
    if not str(entry.get("secret") or ""):
        return Check(f"route:{name}", ABSENT, "registered without a secret",
                     f"{remedy}: a notice without a secret cannot be signed")
    # The registry's own `host` is not checked: the feed always posts to the loop's origin and
    # never reads the registry's (a private PR link must not follow a registry edit), so a stale
    # one changes nothing about delivery.
    if not str(loop.get("host") or ""):
        return Check(f"route:{name}", ABSENT, "the loop names no gateway origin",
                     f"`hermes review-loop set --loop {loop['id']} --host "
                     "https://your-gateway.example`: the feed never borrows the registry's host")
    muted = " · muted" if cfg.get("mute") else ""
    return Check(f"route:{name}", VERIFIED,
                 f"{profile} · observer feed → {contract['deliver']} · deliver-only{muted}")


def check_scripts() -> Check:
    missing = [name for name in PLUGIN_SCRIPTS if not (scripts_dir() / name).exists()]
    if missing:
        return Check("scripts", ABSENT, "missing from the plugin: " + ", ".join(missing),
                     "reinstall the plugin: the routes and the cron shim run these files by name, "
                     "so a missing one is a seat that can never be woken")
    return Check("scripts", VERIFIED, f"{scripts_dir()} (watchdog, three gates, cleanup)")


def gate_timeout_profiles(loop: dict) -> dict[str, list[str]]:
    """Every profile whose gateway runs one of this loop's route scripts → the routes it hosts."""
    hosted: dict[str, list[str]] = {}
    for role in ("reviewer", "fixer", "adjudicator"):
        if role == "adjudicator" and not str((loop.get("adjudicator") or {}).get("route") or ""):
            continue
        profile = config.seat_profile(loop, role)
        if profile:
            hosted.setdefault(profile, []).append(role)
    observer = loop.get("observer") if isinstance(loop.get("observer"), dict) else {}
    if observer.get("route"):
        hosted.setdefault(str(observer.get("profile") or "default"), []).append("observer")
    return hosted


def check_gate_timeouts(loop: dict) -> list[Check]:
    """Does a route script's time budget fit inside its gateway's script timeout (#75)?

    One line per profile that hosts a loop route. The gateway kills a route script at its webhook
    ``script_timeout_seconds`` and answers the delivery 200 "ignored" either way. Gates read the
    same setting (the smaller one, when either the host or the profile's own gateway may serve
    the route) and shrink their budget to fit, so a low value is safe but starves them of time.
    """
    from . import gate_failures as gf
    checks = []
    for profile, roles in gate_timeout_profiles(loop).items():
        name = f"gate:timeout:{profile}"
        serves = f"serves {', '.join(roles)}"
        try:
            home = config.profile_dir(profile)
            limit, rows = gf.effective_timeout(home)
        except Exception as exc:  # noqa: BLE001 - one unreadable profile must not stop doctor
            checks.append(Check(name, UNKNOWN,
                                f"{serves}; the script timeout could not be worked out: "
                                f"{type(exc).__name__}: {exc}"))
            continue
        hosts = "; ".join(f"{label}: {f'{sec}s' if sec is not None else 'unreadable'} ({where})"
                          for label, _host, sec, where in rows)
        unread = [row for row in rows if row[2] is None]
        if unread:
            checks.append(Check(name, UNKNOWN,
                                f"{serves}; cannot read every gateway's script timeout — {hosts}; "
                                f"gates fit the readable ones (or assume "
                                f"{gf.GATEWAY_DEFAULT_TIMEOUT_S}s)"))
            continue
        budget, backstop = gf.plan(limit, gf.DEFAULT_BUDGET_S)
        low = [row for row in rows if row[2] < gf.MIN_TIMEOUT_S]
        tiny = [row for row in rows if row[2] < gf.MIN_RECORDABLE_S]
        if tiny:
            checks.append(Check(
                name, MISMATCH,
                f"{serves}; {hosts} — too small for a gate even to record its own failure (it "
                f"needs at least {gf.MIN_RECORDABLE_S:g}s: {gf.STARTUP_S:g}s to start, "
                f"{gf.RECORD_S:g}s to record, 1s of work); a hung gate is killed with no record",
                "; ".join(f"set platforms.webhook.{gf.KEY}: {gf.GATEWAY_DEFAULT_TIMEOUT_S} (at "
                          f"least {gf.MIN_TIMEOUT_S}) in {host / 'config.yaml'} for the {label}"
                          for label, host, _sec, _where in tiny) + "; then restart that gateway"))
            continue
        if low:
            fixes = "; ".join(
                f"set platforms.webhook.{gf.KEY}: {gf.GATEWAY_DEFAULT_TIMEOUT_S} (at least "
                f"{gf.MIN_TIMEOUT_S}) in {host / 'config.yaml'} for the {label}"
                for label, host, _sec, _where in low)
            checks.append(Check(name, MISMATCH,
                                f"{serves}; {hosts} — gates get only {budget:g}s for GitHub reads "
                                f"(they need {gf.DEFAULT_BUDGET_S:g}s, plus a {gf.BACKSTOP_S:g}s "
                                f"backstop and time to record a failure)",
                                f"{fixes}; then restart that gateway. Until then an overrun is "
                                f"recorded as a gate timeout and re-driven by the watchdog"))
            continue
        checks.append(Check(name, VERIFIED,
                            f"{serves}; {hosts}; gate budget {budget:g}s + {backstop:g}s "
                            f"backstop fits"))
    return checks


_WATCHDOG_LINE = re.compile(r"WATCHDOG\s*=\s*pathlib\.Path\((['\"])(?P<path>.+?)\1\)")


def check_shim(loop: dict) -> Check:
    """The cron shim, and the plugin path it is pinned to.

    The shim is written once, at init, with the *absolute* path of the watchdog that existed
    then. An upgrade that moves the plugin directory leaves a shim pointing at a path that no
    longer runs this code — the scheduler keeps firing it, and the loop keeps looking armed.
    """
    path = shim_path()
    if not path.exists():
        return Check("cron:shim", ABSENT, f"no {path}",
                     f"{shim_fix(loop)}; then, if no job runs it yet: {cron_fix(loop)}")
    try:
        text = path.read_text()
    except Exception as exc:
        return Check("cron:shim", MISMATCH, f"{path} cannot be read ({exc})",
                     f"chmod +r {path}, or {shim_fix(loop)}")
    # Do not run arbitrary installed shim code during a read-only preflight. Instead require
    # byte-for-byte identity with the shim init actually generates; a dead assignment, commented
    # line or altered subprocess invocation cannot pass merely by mentioning WATCHDOG.
    live = str(scripts_dir() / "watchdog.py")
    from . import cli
    expected = cli.SHIM.format(watchdog=pathlib.Path(live))
    if text != expected:
        return Check("cron:shim", MISMATCH, f"{path} differs from init's executable shim for {live}",
                     shim_fix(loop))
    pinned = live
    return Check("cron:shim", VERIFIED, f"{path} → {pinned}")


def check_cron_job(loop: dict) -> Check:
    """The scheduled job itself, read from the scheduler's own store (no scheduler needed)."""
    path = cron_store()
    if not path.exists():
        return Check("cron:job", ABSENT, f"no cron store at {path}",
                     cron_fix(loop))
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return Check("cron:job", MISMATCH, f"{path} is not readable JSON ({exc})",
                     "repair the job store (a store the scheduler cannot read is a watchdog that "
                     f"never sweeps), then, if the job is gone: {cron_fix(loop)}")
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return Check("cron:job", MISMATCH, f"{path} has no job list",
                     f"repair the job store, then, if the job is gone: {cron_fix(loop)}")
    wanted = watchdog_job_name(loop)
    # A pre-#60 install has one per-loop job per loop; each sweeps every loop, so N jobs give
    # N sweeps per tick (#60). Any shim job beyond the single shared one is a duplicate sweep:
    # name them all for migration — remove the extras, keep (or create) one shared job.
    shared = [entry for entry in jobs if isinstance(entry, dict)
              and str(entry.get("name") or "").strip() == wanted]
    shim_jobs = [entry for entry in jobs if isinstance(entry, dict)
                 and pathlib.Path(str(entry.get("script") or "")).name == SHIM_NAME]
    if not shared and shim_jobs:
        ids = ", ".join(str(entry.get("id") or "?") for entry in shim_jobs)
        removals = "; ".join(f"`hermes cron remove {entry.get('id')}`" for entry in shim_jobs)
        return Check("cron:job", MISMATCH,
                     f"{len(shim_jobs)} per-loop watchdog job(s) ({ids}) each sweep every loop "
                     f"(N jobs × N loops = N² sweeps per tick) — migrate to the one shared job",
                     f"{removals}, then {cron_fix(loop)}")
    job = shared[0] if shared else None
    if job is None:
        # A job the operator wrote by hand: same shim, a name that names the loop.
        job = next((entry for entry in jobs if isinstance(entry, dict)
                    and pathlib.Path(str(entry.get("script") or "")).name == SHIM_NAME
                    and (loop["id"] in str(entry.get("name") or "")
                         or str(entry.get("name") or "").strip() == SHARED_JOB_NAME)), None)
    if job is None:
        return Check("cron:job", ABSENT, f"no job named {wanted!r} in {path}",
                     cron_fix(loop))
    job_id = str(job.get("id") or "?")
    named = [str(entry.get("id") or "?") for entry in jobs if isinstance(entry, dict)
             and str(entry.get("name") or "").strip() == wanted]
    if len(named) > 1:
        return Check("cron:job", MISMATCH,
                     f"{len(named)} jobs are named {wanted!r} ({', '.join(named)}) — each one "
                     "fires the watchdog",
                     cron_replace_fix(loop, named))
    # More than one shim job total (a leftover legacy job beside the shared one) is the same
    # N-sweeps-per-tick problem: name the extras so they can be removed.
    if len(shim_jobs) > 1:
        extra = [str(entry.get("id") or "?") for entry in shim_jobs
                 if str(entry.get("id") or "?") != job_id]
        removals = "; ".join(f"`hermes cron remove {extra_id}`" for extra_id in extra)
        return Check("cron:job", MISMATCH,
                     f"{len(shim_jobs)} jobs run {SHIM_NAME} "
                     f"({', '.join(str(e.get('id') or '?') for e in shim_jobs)}) "
                     "— each sweeps every loop (N² sweeps per tick); keep one",
                     removals)
    replace = cron_replace_fix(loop, [job_id])
    # Match the scheduler's runnable predicate: a stored pause timestamp blocks firing even
    # when enabled=True and the display state has already been normalized to "scheduled".
    if (not job.get("enabled", True) or job.get("state") in ("paused", "completed")
            or bool(job.get("paused_at"))):
        state = job.get("state")
        reason = ("completed" if state == "completed" else "paused or disabled")
        # A completed recurring watchdog is not "paused": resuming does not re-arm it. It is
        # replaced — never created beside, which would leave two jobs of one name.
        fix = (replace if state == "completed" else
               f"`hermes cron resume {job_id}`: the scheduler skips a disabled watchdog")
        return Check("cron:job", MISMATCH, f"{job_id} ({wanted}) is {reason}",
                     fix)
    if job.get("script") != SHIM_NAME or job.get("no_agent") is not True:
        return Check("cron:job", MISMATCH,
                     f"{job_id} runs {job.get('script')!r} (no_agent={job.get('no_agent')!r}), "
                     f"expected {SHIM_NAME!r} with --no-agent", replace)
    schedule_data = job.get("schedule")
    valid = False
    missing_croniter = False
    if isinstance(schedule_data, dict):
        kind = schedule_data.get("kind")
        if kind == "interval":
            interval = schedule_data.get("minutes")
            valid = type(interval) is int and interval > 0
        elif kind == "cron":
            expression = schedule_data.get("expr")
            if isinstance(expression, str):
                try:
                    from croniter import croniter
                except ImportError:
                    missing_croniter = True
                else:
                    try:
                        croniter(expression)
                        valid = True
                    except (ValueError, TypeError, KeyError):
                        pass
    if not valid and not missing_croniter:
        return Check("cron:job", MISMATCH,
                     f"{job_id} has no valid stored schedule (display text does not schedule work)",
                     replace)
    next_run = job.get("next_run_at")
    try:
        if not isinstance(next_run, str) or not next_run.strip():
            raise ValueError("missing next run")
        datetime.fromisoformat(next_run.replace("Z", "+00:00"))
    except ValueError:
        return Check("cron:job", MISMATCH,
                     f"{job_id} has no valid next_run_at — cannot verify the next wake "
                     "(the scheduler may recompute a missing value for a recurring job)",
                     replace)
    if missing_croniter:
        return Check("cron:job", UNKNOWN,
                     f"{job_id} cron schedule could not be validated here (croniter unavailable); "
                     "the stored job may be valid — check on the scheduler host")
    schedule = (job.get("schedule_display")
                or ((job.get("schedule") or {}).get("display") if isinstance(job.get("schedule"), dict)
                    else "")
                or "?")
    return Check("cron:job", VERIFIED, f"{job_id} {schedule}, next {next_run}")


def check_clone(loop: dict) -> Check:
    """The clone the runs isolate from — and the one rail that would delete it if it were wrong."""
    raw = str(loop.get("clone") or "")
    if not raw:
        return Check("clone", VERIFIED, "none configured — runs work in the checkout they find "
                                        "(fine at concurrency 1)")
    path = pathlib.Path(raw).expanduser()
    if not path.exists():
        return Check("clone", ABSENT, f"no clone at {path}",
                     f"clone it, or `hermes review-loop set --loop {loop['id']} --clone <path>`: "
                     f"the cleanup prunes worktrees through this path")
    if not path.is_dir() or not (path / ".git").exists():
        return Check("clone", MISMATCH, f"{path} is not a git checkout",
                     f"point `hermes review-loop set --loop {loop['id']} --clone` at the "
                     f"repository's working clone: isolation clones from it and cleanup prunes "
                     f"worktrees in it")
    # The tree the cleanup deletes, derived from config rather than re-spelled here — a clone that
    # lives inside it is a working copy the cleanup would take with the artifacts.
    root = config.artifacts_dir(loop, 1).parent
    try:
        inside = str(path.resolve()).startswith(str(root.resolve()) + os.sep)
    except Exception:
        inside = False
    if inside:
        return Check("clone", MISMATCH, f"{path} lives inside the loop's artifacts root {root}",
                     "move the clone out of the state directory: the cleanup deletes everything "
                     "under artifacts/, which would be your working copy")
    parallel = [seat for seat in ("reviewer", "fixer") if config.seat_concurrency(loop, seat) > 1]
    note = f" — isolation source for {', '.join(parallel)}" if parallel else ""
    return Check("clone", VERIFIED, f"{path} (git checkout){note}")


def _gib(count: int) -> str:
    return f"{count / 1024 ** 3:.1f} GiB"


def _host_memory() -> tuple[int, int] | None:
    """(available, total) bytes from /proc/meminfo, or None where there is no /proc.

    Available is what a new allocation can take: reclaimable memory plus free swap. Total is what
    the machine has at all, for the operator's context when the two are far apart.
    """
    fields: dict[str, int] = {}
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            name, _, rest = line.partition(":")
            fields[name] = int(rest.split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    total = fields.get("MemTotal")
    if total is None:
        return None
    return fields.get("MemAvailable", 0) + fields.get("SwapFree", 0), total + fields.get("SwapTotal", 0)


def check_sandbox_caps(loop: dict) -> Check:
    """What the seats' writable mounts can reach, against the memory that has to back them.

    Both mounts are tmpfs, so they are charged against RAM and swap rather than disk: a cap sized
    for one turn can still take a small host down when several seats run at once. This reports the
    worst case at *this* loop's concurrency beside what the host has available.
    """
    from . import contained
    per_turn = contained.CHECKOUT_SIZE + contained.SCRATCH_SIZE
    turns = (config.seat_concurrency(loop, "reviewer")
             + config.seat_concurrency(loop, "fixer") + 1)
    worst = per_turn * turns
    detail = (f"/work {_gib(contained.CHECKOUT_SIZE)} + /tmp {_gib(contained.SCRATCH_SIZE)}"
              f" = {_gib(per_turn)} per turn, {_gib(worst)} at {turns} concurrent turns")
    refused_overrides = contained.live_ignored_overrides()
    if refused_overrides:
        refused = ", ".join(f"REVIEW_LOOP_{name}_GIB={raw!r} ({why})"
                            for name, raw, why in refused_overrides)
        return Check("sandbox:caps", MISMATCH, f"{detail} — with an override refused: {refused}",
                     "fix the value (an integer 1..1024 GiB) and restart the gateway: the launcher "
                     "reads it once, when the supervisor imports it")
    memory = _host_memory()
    if memory is None:
        return Check("sandbox:caps", VERIFIED, f"{detail} (host memory unreadable here)")
    available, total = memory
    if worst > available:
        return Check("sandbox:caps", MISMATCH,
                     f"{detail} — more than the {_gib(available)} available of {_gib(total)}",
                     "lower the caps (REVIEW_LOOP_CHECKOUT_SIZE_GIB / "
                     "REVIEW_LOOP_SCRATCH_SIZE_GIB), add memory, or let the seats run one at a "
                     "time: tmpfs is charged against RAM and swap")
    return Check("sandbox:caps", VERIFIED, f"{detail} — {_gib(available)} available of {_gib(total)}")


def check_fixer_push(loop: dict) -> Check:
    """Whether the fix leg can run at all. Off is the safe default, not a fault — but it means
    every changes-requested verdict is held for the operator instead of starting a fixer turn."""
    if config.unattended_fixer_push_enabled(loop):
        return Check("fixer-push", VERIFIED,
                     "enabled — a changes-requested verdict starts an isolated fixer turn that "
                     "can publish one push and re-request review")
    return Check("fixer-push", UNKNOWN,
                 "off — the fix leg cannot run: changes-requested verdicts are held for you and no "
                 f"fixer turn starts. To opt in: `{config.fixer_push_enable_command(loop)}` "
                 "(held verdicts then start on the next watchdog sweep)",
                 f"`{config.fixer_push_enable_command(loop)}`")


def check_state_dir(loop: dict) -> Check:
    path = pathlib.Path(str(loop["state_dir"])).expanduser()
    if path.exists() and not path.is_dir():
        return Check("state_dir", MISMATCH, f"{path} is not a directory",
                     "point state_dir at a directory: the locks, the queue and the artifacts all "
                     "live under it")
    parent = _nearest_dir(path) or path.parent
    if not os.access(parent, os.W_OK):
        return Check("state_dir", MISMATCH, f"{parent} is not writable",
                     f"make {parent} writable by the user the gateway runs as: the seat locks and "
                     f"the queue live under {path}")
    when = "exists" if path.exists() else f"created under {parent} on the first run"
    return Check("state_dir", VERIFIED, f"{path} ({when})")


def check_roots(loop: dict) -> Check:
    roots = [str(root) for root in (loop.get("roots") or [])]
    if not roots:
        return Check("roots", VERIFIED, "none configured — the cleanup reclaims nothing "
                                        "(runs are unaffected)")
    wrong = [root for root in roots
             if pathlib.Path(root).expanduser().exists()
             and not pathlib.Path(root).expanduser().is_dir()]
    if wrong:
        return Check("roots", MISMATCH, "not directories: " + ", ".join(wrong),
                     "fix them (re-run init with --root <dir>): the cleanup only ever deletes "
                     "inside a configured root")
    return Check("roots", VERIFIED, f"{len(roots)} configured: " + ", ".join(roots))


def gateway_reachable(host: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Is anything listening at the gateway's origin? A TCP connect — never a webhook POST,
    because a POST at a seat's route starts a real agent run."""
    parsed = urlsplit(host)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    name = parsed.hostname or host
    # Under the test guard only a loopback gateway is probed; a real host raises first.
    config.guard_network(f"tcp://{f'[{name}]' if ':' in name else name}:{port}")
    try:
        with socket.create_connection((name, port), timeout=timeout):
            return True, f"{name}:{port} accepts a connection"
    except Exception as exc:
        return False, f"{name}:{port} — {type(exc).__name__}: {exc}"


def check_gateway(loop: dict, offline: bool) -> Check:
    host = str(loop.get("host") or "")
    if not host:
        return Check("gateway", ABSENT, "no webhook host configured",
                     "pass --host https://your-gateway.example at init (or set the plugin's "
                     "webhook host): without one no route URL resolves and no hook can point at "
                     "this operator's gateway")
    if offline:
        return Check("gateway", UNKNOWN, "configured gateway not probed (--offline; URL withheld)")
    reachable, _ = gateway_reachable(host)
    if not reachable:
        return Check("gateway", ABSENT, "configured gateway unreachable (URL withheld)",
                     "start it (`hermes gateway status`, `hermes gateway start`), and check that "
                     "this origin is the one GitHub posts to; re-run with --offline to skip this "
                     "probe")
    return Check("gateway", VERIFIED, "configured gateway accepts a TCP connection (URL withheld)")


def check_hooks(loop: dict, offline: bool) -> list[Check]:
    """The two repo hooks GitHub posts to, matched by the URL each route resolves to.

    A read the token is not allowed to make is ``unknown``: the repo may have both hooks and
    none at all, and this call cannot tell those apart. Saying "absent" here would be the exact
    wrong-hook hunt the preflight exists to prevent.
    """
    expected = []
    for seat in ("reviewer", "fixer"):
        name = str(loop["seats"][seat].get("route") or "")
        try:
            url = routes.url_for(name, str(loop.get("host") or "") or None) if name else None
        except config.ConfigError:
            url = None
        if url:
            expected.append((seat, name, url))
    if not expected:
        return [Check("hooks", UNKNOWN, "not checked — no route URL resolves yet "
                                        "(see the route checks above)")]
    if offline:
        return [Check("hooks", UNKNOWN,
                      f"not probed (--offline) — {len(expected)} hook(s) were not read")]
    hooks = []
    complete = False
    for page in range(1, 1001):
        path = f"/repos/{loop['repo']}/hooks?per_page=100"
        batch = gh.api(loop, path if page == 1 else f"{path}&page={page}")
        if not isinstance(batch, list) or any(
            not isinstance(hook, dict) or not isinstance(hook.get("config"), dict)
            or not isinstance(hook["config"].get("url"), str)
            or not isinstance(hook.get("events"), list)
            or any(not isinstance(event, str) for event in hook["events"])
            or type(hook.get("active")) is not bool
            for hook in batch):
            break
        hooks.extend(batch)
        if len(batch) < 100:
            complete = True
            break
    if not complete:
        return [Check("hooks", UNKNOWN,
                      f"could not read the complete /repos/{loop['repo']}/hooks listing — nothing was proved about "
                      f"{len(expected)} hook(s) (a token without hook read access — `repo`, or the "
                      f"narrower `read:repo_hook` — reads as denied)",
                      f"give the read token hook read access — `repo`, or the narrower "
                      f"`read:repo_hook` — and re-run; check by hand with "
                      f"`gh api repos/{loop['repo']}/hooks`")]
    checks = []
    for seat, name, url in expected:
        checks.append(check_hook(loop, hooks, seat, name, url))
    return checks


def hook_route_name(hook: dict) -> str:
    """The webhook route a hook posts to: the last ``/webhooks/<name>`` path segment, exactly.

    Never a substring test on the URL: route ``widgets`` must not claim a hook posting to
    ``widgets-fix``, nor a route name embedded anywhere else in a URL.
    """
    url = (hook.get("config") or {}).get("url") if isinstance(hook.get("config"), dict) else ""
    return routes.route_name_of(url)


def seat_route_target(loop: dict, name: str) -> tuple[str | None, str]:
    """``(url, "")`` for the one URL a hook must post to for route ``name`` to wake its seat, or
    ``(None, reason)`` when no hook can.

    The registry is the gateway's binding, so it is the only source: the gateway serves
    ``/webhooks/<route>`` (default profile) and ``/p/<profile>/webhooks/<route>`` for a route in
    its subscription file, and answers anything else 404 — another profile's URL included. A
    route missing from the registry has no binding at all; a route whose entry binds another
    profile than the seat's would run the wrong agent. Both are ``doctor.check_route``'s findings,
    in its words, and neither has a URL a hook could be credited to.
    """
    role = next((role for role, route in route_intent.routes_of(loop).items() if route == name),
                "")
    entry = routes.route(name)
    if entry is None:
        return None, (f"route {name!r} is not in {routes.subs_path().name} — the gateway has no "
                      "binding for it, so no hook can wake this seat")
    want = config.seat_profile(loop, role) if role else ""
    if role in ("reviewer", "fixer") and str(entry.get("profile") or "") != want:
        return None, (f"route {name!r} wakes profile {entry.get('profile')!r}, but "
                      f"seats.{role}.profile is {want!r} — the wake would run the wrong agent")
    url = routes.url_for(name, str(loop.get("host") or "") or None)
    if not url:
        return None, f"route {name!r} has no gateway host to build its URL from"
    return url, ""


def seat_hook_url(loop: dict, name: str) -> str | None:
    """The registry URL a hook must post to for route ``name`` (see ``seat_route_target``)."""
    return seat_route_target(loop, name)[0]


def same_hook_url(posted: str, expected: str) -> bool:
    """Is this hook *for* that route URL? Scheme and host case-insensitive, a trailing slash
    ignored. For finding a hook (whose is it, which one to pause or delete) — never for deciding
    it is correct: see ``exact_hook_url``."""
    return routes.same_webhook_url(posted, expected)


def exact_hook_url(posted: str, expected: str) -> bool:
    """Does the gateway route this hook's URL to that route? Path byte-exact.

    The gateway registers ``/webhooks/{route_name}`` and ``/p/{profile}/webhooks/{route_name}``
    only (``{route_name}`` cannot hold a ``/``, and no path normalizing is installed), so
    ``…/webhooks/<route>/`` falls through to the ``/p/{profile}/{tail}`` catch-all and is
    answered 404. Scheme and host still compare case-insensitively (DNS and the Host header),
    and a query string is not part of the match: the router reads the path only, so
    ``…/webhooks/<route>?x=1`` is delivered like the bare URL.
    """
    return routes.serves_route_url(posted, expected)


SLASH_404 = ("posts to the route's URL with a trailing slash — the gateway does not route it "
             "(404), so this seat is never woken")


def _profile_in(path: str) -> str:
    """The profile a webhook URL path names (``default`` when it has no ``/p/<profile>``)."""
    head = path.rsplit("/webhooks/", 1)[0].strip("/")
    return head[2:] if head.startswith("p/") else ("default" if not head else "")


def hook_url_difference(posted: str, expected: str) -> str:
    """Why ``posted`` is not the route's own URL, in the operator's terms (no URL echoed)."""
    if not expected:
        return "a route with no registry binding (see the route's line)"
    got, want = urlsplit(posted.rstrip("/")), urlsplit(expected.rstrip("/"))
    if (got.scheme.lower(), got.netloc.lower()) != (want.scheme.lower(), want.netloc.lower()):
        return f"another origin ({got.scheme}://{got.netloc.lower()}, not this loop's gateway)"
    have, need = _profile_in(got.path), _profile_in(want.path)
    if have and need and have != need and got.path.endswith(want.path.rsplit("/webhooks/", 1)[-1]):
        return (f"another profile ({have!r}; the route is bound to {need!r}, and the gateway "
                "answers any other profile's URL 404)")
    return "another path on this gateway (not the route's URL)"


def install_hook_urls(loop: dict, name: str) -> list[str]:
    """Every URL a hook *this install* made (or is about to make) for route ``name`` posts to.

    An ownership question, not a delivery one: pausing (``arm --pause``), ``uninstall``
    deleting its hooks (``cli._classify_hooks``) and ``init``'s stale-hook guard (the same
    function) ask "is this hook one of ours?", and the answer includes the route's registry URL
    whatever profile it binds, and the URL the loop's config gives it — which is all there is
    once the registry entry is gone (a route removed before its hooks, or for ``init`` not
    written yet). Arming never uses this: whether a hook *wakes the
    seat* is ``seat_route_target``'s question, and only the registry binding answers it.
    """
    urls = []
    host = str(loop.get("host") or "") or None
    if routes.route(name) is not None:
        registered = routes.url_for(name, host)
        if registered:
            urls.append(registered)
    role = next((role for role, route in route_intent.routes_of(loop).items() if route == name),
                "")
    if role:
        planned = routes.url_for_profile(name, config.seat_profile(loop, role), host)
        if planned and not any(same_hook_url(planned, url) for url in urls):
            urls.append(planned)
    return urls


def hook_wake_problem(hook: dict, seat: str) -> str:
    """Why a hook at the seat's own URL still would not wake it, in ``check_hook``'s words."""
    event = GATE_EVENT.get(seat)
    events = hook.get("events")
    if not isinstance(events, list) or event not in [str(item) for item in events]:
        return (f"subscribes to {events if isinstance(events, list) and events else '(no events)'}"
                f", not {event!r}")
    content_type = (hook.get("config") or {}).get("content_type")
    if content_type != "json":
        return f"has content_type {content_type!r}, expected 'json'"
    return ""


def split_route_hooks(loop: dict, listing: list, names, *,
                      ownership: bool = False) -> tuple[list[dict], list[dict]]:
    """``(own, other)`` for the hooks posting to one of the route ``names``.

    The one matcher for hooks: ``arm`` (arming and pausing), ``uninstall`` and ``init``'s
    stale-hook guard (via ``cli._classify_hooks``) and ``selftest --ping``; ``doctor``'s helpers
    use the same URL rules. A hook is a
    seat's only when it posts to exactly that route's registry URL, and the registry entry binds
    the seat's profile (``seat_route_target``). Every other hook
    whose last ``/webhooks/<route>`` segment names one of the routes — another profile's URL, a
    retired gateway, another install — is *other*: reported, never flipped, deleted or pinged
    as this loop's. ``ownership=True`` (pausing, uninstall, init's stale-hook guard) counts the
    URLs ``install_hook_urls`` names instead. Raises ``ConfigError`` when the loop has no usable
    host.
    """
    config.webhook_host(loop.get("host"), required=True)
    if ownership:
        expected = {name: install_hook_urls(loop, name) for name in set(names)}
    else:
        expected = {name: [url] if (url := seat_hook_url(loop, name)) else []
                    for name in set(names)}
    own, other = [], []
    for hook in listing:
        if not isinstance(hook, dict):
            continue
        name = hook_route_name(hook)
        if name not in expected:
            continue
        posted = str((hook.get("config") or {}).get("url") or "")
        mine = any(same_hook_url(posted, want) for want in expected[name])
        (own if mine else other).append(hook)
    return own, other


def check_hook(loop: dict, hooks: list, seat: str, name: str, url: str) -> Check:
    event = GATE_EVENT[seat]
    def posted_url(hook: dict) -> str:
        cfg = hook.get("config")
        return str(cfg.get("url") or "") if isinstance(cfg, dict) else ""

    exact = [hook for hook in hooks if same_hook_url(posted_url(hook), url)]
    # A wrong origin/profile/path for the same webhook route is a mismatch (reported with its
    # cause), not an absent hook — "the same route" is the exact segment, never a substring.
    candidates = exact or [hook for hook in hooks if hook_route_name(hook) == name]
    # Every hook posting to this route name, on any origin. More than one is a previous install
    # left behind: GitHub never returns a secret, but a leftover signs with the secret the old
    # route held, so at most one of them can authenticate — and "hook N active" says nothing
    # about which one that is.
    # Only this gateway's hooks can be duplicates: the same route name on another origin is
    # another install (or an old gateway) and never receives this route's deliveries.
    # A duplicate is a second hook at the route's own URL; one at another profile, path or
    # origin never reaches this route at all (the gateway answers it 404, or it goes elsewhere).
    named = list(exact)
    if len(named) > 1:
        ids = sorted(hook.get("id") for hook in named if isinstance(hook.get("id"), int))
        active = sum(1 for hook in named if hook.get("active"))
        repo = loop["repo"]
        deletes = "; ".join(f"`gh api -X DELETE {shlex.quote(f'repos/{repo}/hooks/{i}')}`"
                            for i in ids[:-1])
        return Check(f"hook:{name}", MISMATCH,
                     f"{len(named)} repo hooks post to this route (ids "
                     f"{', '.join(str(i) for i in ids)}; {active} active) — duplicates from a "
                     "previous install sign with a secret this route no longer holds, so their "
                     "deliveries are refused",
                     f"`hermes review-loop uninstall --loop {shlex.quote(loop['id'])}` deletes "
                     "them all, then re-run init --hooks; or keep only the newest (GitHub ids "
                     f"only grow, so {ids[-1]} is the latest init's) and delete the rest: {deletes}")
    match = next((hook for hook in candidates if hook.get("active") and
                  event in (hook.get("events") or [])), None) or (candidates[0] if candidates else None)
    if match is None:
        return Check(f"hook:{name}", ABSENT, "no repo hook posts to [webhook URL redacted]",
                     hooks_fix(loop, "creates it, paused until `arm`") + ", or add the hook "
                     "by hand with that URL and the route's secret")
    hook_id = match.get("id")
    posted = posted_url(match)
    if not same_hook_url(posted, url):
        why = hook_url_difference(posted, url)
        return Check(f"hook:{name}", MISMATCH,
                     f"hook {hook_id} posts to {why}, not [webhook URL redacted]",
                     hooks_fix(loop, f"repoints hook {hook_id} at the route's URL (secret and TLS "
                                     "setting kept): the gateway delivers this route only at that "
                                     "URL, so the hook wakes nothing"))
    if not exact_hook_url(posted, url):
        return Check(f"hook:{name}", MISMATCH, f"hook {hook_id} {SLASH_404}",
                     f"`hermes review-loop apply --loop {loop['id']}` repoints hook {hook_id} at "
                     "the exact URL (or edit its URL on GitHub to drop the trailing slash)")
    events = [str(item) for item in (match.get("events") or [])]
    if event not in events:
        return Check(f"hook:{name}", MISMATCH,
                     f"hook {hook_id} subscribes to {events or '(no events)'}, not {event!r}",
                     hooks_fix(loop, f"adds {event!r} to hook {hook_id}"))
    content_type = match["config"].get("content_type")
    if content_type != "json":
        return Check(f"hook:{name}", MISMATCH,
                     f"hook {hook_id} has content_type {content_type!r}, expected 'json'",
                     hooks_fix(loop, f"sets hook {hook_id}'s content_type to json: the gate reads a "
                                     "JSON payload, not form-encoded data"))
    delivery = check_deliveries(loop, hook_id, name)
    if isinstance(delivery, Check):
        return delivery
    if not match.get("active"):
        return Check(f"hook:{name}", VERIFIED,
                     f"hook {hook_id} → [webhook URL redacted] ({event}, PAUSED — nothing fires "
                     f"until `hermes review-loop arm --loop {loop['id']}`; {delivery})", paused=True)
    return Check(f"hook:{name}", VERIFIED,
                 f"hook {hook_id} → [webhook URL redacted] ({event}, active; {delivery})")


# The gateway's answers to a delivery whose signature it would not accept: 401 is "Invalid
# signature", 403 a route that is disabled or has no HMAC secret to check against.
REJECTED = {401: "signature rejected — the hook's secret does not match the route's",
            403: "refused — the route is disabled or holds no secret"}


def check_deliveries(loop: dict, hook_id, name: str) -> "Check | str":
    """How the gateway answered this hook's most recent delivery — GitHub's only secret evidence.

    GitHub never returns a hook's secret, but it keeps each recent delivery with the status code
    the gateway answered. A latest delivery answered 401/403 is a hook signing with a secret the
    route does not hold (a previous install's, say): it looks armed and wakes nothing. Returns a
    failing/unknown ``Check``, or a short phrase for the verified line.
    """
    path = f"/repos/{loop['repo']}/hooks/{hook_id}/deliveries?per_page=30"
    data, error = gh.fetch(loop, path)
    if error or not isinstance(data, list) or not all(isinstance(d, dict) for d in data):
        reason = error or "no delivery list returned"
        return Check(f"hook:{name}", UNKNOWN,
                     f"hook {hook_id} found, but its recent deliveries could not be read ({reason}) "
                     "— whether its secret matches the route is unproven",
                     f"give the read token hook read access (`read:repo_hook`, or `repo`), or look "
                     f"by hand: `gh api repos/{loop['repo']}/hooks/{hook_id}/deliveries`")
    stamped = [d for d in data if isinstance(d.get("delivered_at"), str)]
    if not stamped:
        return "no deliveries yet — the secret is unproven until the first one arrives"
    latest = max(stamped, key=lambda d: d["delivered_at"])
    code = latest.get("status_code")
    deliveries = f"`gh api repos/{loop['repo']}/hooks/{hook_id}/deliveries`"
    ping = f"`hermes review-loop selftest --loop {shlex.quote(loop['id'])} --no-model --ping`"
    if type(code) is not int or code <= 0:
        # GitHub recorded the delivery but no HTTP answer (a timeout, a refused connection): the
        # gateway never judged the signature, so nothing is proven — hook_ping refuses this too.
        return Check(f"hook:{name}", UNKNOWN,
                     f"hook {hook_id}'s latest delivery ({latest['delivered_at']}) got no HTTP "
                     f"response from the gateway (GitHub recorded: "
                     f"{latest.get('status') or 'no status'}) — it never answered, so whether "
                     "its secret matches is unproven",
                     f"check the gateway is running and reachable from GitHub (`hermes gateway "
                     f"status`), then {ping}; the delivery log: {deliveries}")
    if code in REJECTED:
        return Check(f"hook:{name}", MISMATCH,
                     f"hook {hook_id}'s latest delivery ({latest['delivered_at']}) got HTTP {code}: "
                     f"{REJECTED[code]}, so the hook wakes nothing",
                     f"`hermes review-loop uninstall --loop {shlex.quote(loop['id'])}` (deletes the "
                     f"hook), then re-run init --hooks so the new hook and route share one fresh "
                     f"secret; then `hermes review-loop arm --loop {shlex.quote(loop['id'])}`")
    if not 200 <= code < 300:
        # A 5xx is the gateway erroring on the delivery (and any other non-2xx is it refusing):
        # either way nothing woke, and the signature was never shown to verify.
        kind = "the gateway errored" if code >= 500 else "the gateway did not accept it"
        return Check(f"hook:{name}", MISMATCH,
                     f"hook {hook_id}'s latest delivery ({latest['delivered_at']}) got HTTP {code}: "
                     f"{kind}, so the hook wakes nothing and its secret is unproven",
                     f"read the gateway's log for that delivery ({deliveries}), fix what it "
                     f"reports, then {ping}")
    return f"latest delivery {code}"


# -- the report ------------------------------------------------------------------

_URL_IN_REPORT = re.compile(r"https?://[^\s`<>]+", re.IGNORECASE)


def _safe_report_text(text: str) -> str:
    """Never echo a configured URL: userinfo, path, query and fragment may all be secrets."""
    return _URL_IN_REPORT.sub("[webhook URL redacted]", text)


def check_gateway_scripts(loop: dict) -> list[Check]:
    """Each installed route's script, resolved exactly as the gateway resolves it (issue #105):
    under the serving profile's ``scripts/``, a real file inside it, and this plugin's shim."""
    from . import gate_shims
    status_of = {"ok": VERIFIED, "absent": ABSENT, "mismatch": MISMATCH}
    return [Check(name, status_of[status], detail, fix)
            for name, status, detail, fix in gate_shims.live_checks(loop)]


def check_loop(loop: dict, offline: bool = False) -> list[Check]:
    """Every check, in the order an operator reads an install: what it is, who runs it, what
    wakes it, what schedules it, and where it works."""
    checks = [check_config(loop), check_turn_budget(loop)]
    for seat in ("reviewer", "fixer"):
        checks.append(check_profile(loop, seat))
        checks.append(check_credential(loop, seat))
    if str((loop.get("adjudicator") or {}).get("route") or ""):
        checks.append(check_adjudicator_profile(loop))
    checks.extend(check_seat_models(loop))
    checks.extend(check_seat_extras(loop))
    checks.append(check_fixer_push(loop))
    checks.append(check_sandbox_caps(loop))
    identity = check_adjudicator_identity(loop)
    if identity:
        checks.append(identity)
    checks.extend(check_tokens(loop))
    checks.append(check_read_token(loop))
    checks.extend(check_routes(loop))
    checks.extend(check_gateway_scripts(loop))
    checks.append(check_scripts())
    checks.extend(check_gate_timeouts(loop))
    checks.append(check_shim(loop))
    checks.append(check_cron_job(loop))
    checks.append(check_watchdog_last_run(loop))
    checks.extend(check_runtime_paths(loop))
    checks.append(check_clone(loop))
    checks.append(check_state_dir(loop))
    checks.append(check_roots(loop))
    checks.append(check_gateway(loop, offline))
    checks.extend(check_hooks(loop, offline))
    return checks


def report(loop: dict, checks: list[Check], strict: bool = False) -> int:
    """Print one loop's preflight. Returns 1 when something has to be fixed, else 0."""
    failed = [check for check in checks if check.failed]
    unknown = [check for check in checks if check.status == UNKNOWN]
    verified = [check for check in checks if check.status == VERIFIED]

    print()
    print(f"[{loop['id']}] {loop['repo']} — preflight "
          f"(read-only: it writes nothing and fires nothing)")
    for check in checks:
        print(f"  {MARKS[check.status]} {check.name:<20} {_safe_report_text(check.detail)}")
        if check.failed and check.fix:
            print(f"      fix: {_safe_report_text(check.fix)}")
    print()
    skipped = [check for check in checks if check.status == SKIPPED]
    print(f"{loop['id']}: {len(verified)} verified, {len(failed)} failed, {len(unknown)} unknown"
          + (f", {len(skipped)} skipped" if skipped else "") + f" (of {len(checks)} checks)")
    if failed:
        print(f"  {len(failed)} failed: {', '.join(check.name for check in failed)} — "
              f"fix the ❌ lines above before this loop is armed.")
    elif unknown:
        print(f"  no failures — but {len(unknown)} check(s) could not be decided from here; "
              f"verify the ⚠️ lines by hand.")
    elif not any(check.paused for check in checks):
        print("  every check passed — this loop can wake a seat and post a verdict.")
    else:
        print("  every check passed.")
    if not failed and any(check.paused for check in checks):
        print("  the repo hooks are paused, so nothing fires yet: run selftest, then "
              f"`hermes review-loop arm --loop {loop['id']}`.")
    if strict and unknown and not failed:
        print(f"  --strict: {len(unknown)} undecided check(s) count as a failure.")
    return 1 if failed or (strict and unknown) else 0
