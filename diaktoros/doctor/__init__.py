"""Read-only preflight — *can this installation run the loop at all?*

``init`` writes a loop config, three routes and (optionally) two GitHub hooks and a cron job.
Every one of those can be syntactically perfect while the installation still cannot run: the
reviewer's profile does not exist, the token file named for the fixer went away in a key
rotation, the route in the gateway registry wakes a *different* profile than the loop config
says, the repo hook points at the operator's previous gateway, or the cron shim is still pinned
to the plugin directory a previous upgrade left behind. A loop that looks armed and cannot wake
a seat — or cannot post a verdict — is the failure this plugin exists to make loud, so the
preflight answers it before anyone arms anything:

    hermes dk doctor --loop name

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
import subprocess
from typing import NamedTuple

from .. import ci, config, gh, isolation, route_intent

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
SHIM_NAME = "diaktoros-watchdog.py"      # a fresh install's; see shim_path() for the live one
# Every file a route runs by name (route_intent.GATE_SCRIPT, observe.py included) plus the
# watchdog and cleanup scripts, so a route script cannot be added without doctor checking it.
PLUGIN_SCRIPTS = ("watchdog.py", *dict.fromkeys(route_intent.GATE_SCRIPT.values()), "cleanup.py")
GATE_SCRIPT = {"reviewer": "gate_reviewer.py", "fixer": "gate_fixer.py",
               "triage": "gate_triage.py"}
GATE_EVENT = config.HOOK_EVENT


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
    return pathlib.Path(__file__).resolve().parents[2]


def scripts_dir() -> pathlib.Path:
    """Where the plugin's own gate/watchdog/cleanup scripts live — read at call time, so a test
    can point the check at an empty directory without touching the installed plugin."""
    return plugin_root() / "scripts"


def profile_dir(name: str) -> pathlib.Path:
    """A seat profile's home: the root itself for ``default``, else ``profiles/<name>``.

    That is the layout the gateway uses, so "does this profile exist" is answerable offline and
    without asking the gateway anything.
    """
    if not name or name == "default":
        return config.home()
    return config.home() / "profiles" / name


SHARED_JOB_NAME = "diaktoros watchdog"  # one shared job runs the shim (#60); see watchdog_job_name


def shim_path() -> pathlib.Path:
    return config.watchdog_shim()


def cron_store() -> pathlib.Path:
    """The scheduler's job store. Its own file, so the preflight needs no scheduler running."""
    return config.home() / "cron" / "jobs.json"


def watchdog_job_name(loop: dict) -> str:
    """The shared job name — one job sweeps every loop (#60). ``loop`` is accepted for callers."""
    return config.watchdog_job_name()


def cron_fix(loop: dict, deliver: str = "local") -> str:
    """The scheduler's own command for the watchdog job. Never ``init``: it refuses a loop that
    already exists, and the job is the scheduler's to create. ``deliver`` keeps a known target."""
    return (f"`hermes cron create 15m --name \"{watchdog_job_name(loop)}\" --no-agent "
            f"--script {shim_path().name} --deliver {shlex.quote(deliver or 'local')}`")


def _job_schedule(entry: dict) -> str:
    """A stored job's schedule in the form ``hermes cron create`` takes (``30m``, a cron expr)."""
    sched = entry.get("schedule")
    if isinstance(sched, dict):
        minutes = sched.get("minutes")
        if sched.get("kind") == "interval" and isinstance(minutes, int) and minutes > 0:
            return f"{minutes}m"
        expr = sched.get("expr")
        if isinstance(expr, str) and expr.strip():
            return expr.strip()
    elif isinstance(sched, str) and sched.strip():
        return sched.strip()
    return "15m"


def _schedule_label(entry: dict) -> str:
    """``every 30m`` for an interval schedule, the bare expression for a cron one."""
    schedule = _job_schedule(entry)
    return f"every {schedule}" if re.fullmatch(r"\d+[smhd]", schedule) else schedule


def _job_deliver(entry: dict) -> str:
    deliver = entry.get("deliver")
    return deliver.strip() if isinstance(deliver, str) and deliver.strip() else "local"


def migration_fix(loop: dict, shim_jobs: list) -> str:
    """Remove the per-loop jobs, then create the shared one with the schedule and deliver target
    the existing job(s) had. Jobs that disagree are named, not guessed between."""
    removals = "; ".join(f"`hermes cron remove {e.get('id')}`" for e in shim_jobs)
    values = {(_job_schedule(e), _job_deliver(e)) for e in shim_jobs}
    name = watchdog_job_name(loop)
    if len(values) == 1:
        schedule, deliver = next(iter(values))
        return (f"{removals}, then `hermes cron create {shlex.quote(schedule)} --name "
                f"\"{name}\" --no-agent --script {shim_path().name} "
                f"--deliver {shlex.quote(deliver)}`")
    named = "; ".join(f"{e.get('id') or '?'}: {_schedule_label(e)}, deliver "
                      f"{_job_deliver(e)}" for e in shim_jobs)
    return (f"the per-loop jobs disagree ({named}) — choose one schedule and one deliver "
            f"target, then {removals}, then `hermes cron create <schedule> --name "
            f"\"{name}\" --no-agent --script {shim_path().name} --deliver <target>`")


def _job_ref(entry: dict, fallback: str) -> str:
    """How a remedy addresses a job: its id, else (a hand-edited store) its name, else the
    watchdog's name; never a literal ``?`` that ``hermes cron remove`` would reject."""
    return str(entry.get("id") or "").strip() or str(entry.get("name") or "").strip() or fallback


def cron_replace_fix(loop: dict, job_ids, deliver: str = "local") -> str:
    """For a watchdog job that exists but cannot run as it should: remove it (every one, by its
    exact id), then create it. ``hermes cron create`` only appends — it never replaces a job of
    the same name — so printing a bare create here would leave the broken job answering beside
    a duplicate that fires the same shim."""
    removes = ", then ".join(f"`hermes cron remove {job_id}`" for job_id in job_ids)
    return f"{removes}, then {cron_fix(loop, deliver)}"


def shim_fix(loop: dict) -> str:
    return (f"`hermes dk apply --loop {loop['id']} --watchdog-shim` rewrites it from the "
            "plugin (the scheduled job runs it by name)")


def hooks_fix(loop: dict, what: str) -> str:
    """``apply --hooks`` reconciles this loop's repo hooks (every role in ``config.hook_roles``) with
    its routes; ``init --hooks`` refuses an existing loop."""
    from ..cli import common as cli_common   # the one wording of what a hook write needs (#106)
    return (f"`hermes dk apply --loop {loop['id']} --hooks --admin-token <login>` {what} "
            f"({cli_common.hook_write_need_any(loop)})")


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
                     "write one with `hermes dk init`: the gates select a loop by the "
                     "payload's repository, so a loop nobody can load drives nothing")
    try:
        json.loads(path.read_text())
    except Exception as exc:
        return Check("config", MISMATCH, f"{path} is not readable JSON ({exc})",
                     "edit the loop file to repair it, then run `hermes dk apply --loop <id>`: every gate reads it on every event")
    return Check("config", VERIFIED,
                 f"{path} (repo {loop['repo']}, cap {loop['cap']}, base {loop['base']})")


def check_duplicate_repo(loop: dict) -> Check | None:
    """Two loop ids for one repo make ``by_repo`` raise in every gate: events are dropped."""
    others = config.loop_ids_for_repo(loop["repo"], exclude=loop["id"])
    if not others:
        return None
    return Check("duplicate-repo", MISMATCH,
                 f"{loop['repo']} is also configured as loop {', '.join(others)}: every gate "
                 "refuses a repo with two loops, so its events are dropped",
                 "keep one loop per repo: remove the extra config file(s) from "
                 f"{config.config_dir()}")


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
    steps = " · ".join(f"{seat} {config.max_steps(loop, seat)}" for seat in seats)
    text = (f"{detail} per isolated turn (sandbox killed past it); agent steps {steps}; {whole} "
            f"— grace_min {grace_min}m, ttl_min {ttl_min}m; {stall}; {lock}")
    if "adjudicator" in seats:
        # The breach marker's stall clocks (#98): a ruling in flight is never a stall; one
        # claimed with no live run is, only after the adjudicator's whole turn.
        marker = int(loop.get("marker_grace_min") or config.DEFAULTS["marker_grace_min"])
        text += (f"; breach marker: awaiting-adjudication stalls after {marker}m; adjudicating "
                 "only with no live ruling run, after "
                 f"{minutes(config.adjudicating_stall_s(loop))}m")
    return Check("turn-budget", VERIFIED, text)


def check_runtime_paths(loop: dict) -> list[Check]:
    """Validate every path the runtime file names. (#67)

    ``diaktoros-runtime.json`` names four host paths (``source``, ``venv``, ``runtime``,
    ``rust``) that the isolated worker mounts. A Hermes upgrade that moves the install
    leaves those paths pointing at directories that no longer exist — and the worker fails
    on every turn while doctor says nothing. Each path is checked independently so the
    operator sees exactly which one is wrong; a missing runtime file is reported as such.
    """
    from .. import runtime_detect, seat_model
    path = config.host_path("runtime")
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
                      f"repair {path}; `hermes dk selftest` shows each problem")]
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
        elif key == "source" and (not (p / "run_agent.py").is_file()
                                  or not (p / ".git").exists()):
            # Same requirement as `selftest`: every isolated turn snapshots this directory
            # with git and refuses anything that is not a committed Git checkout.
            checks.append(Check(f"runtime:{key}", MISMATCH,
                                f"{key} = {raw} is not a hermes-agent Git checkout "
                                "(run_agent.py and .git required)",
                                f"point {key} at the hermes-agent Git checkout"))
        elif key == "source" and runtime_detect.source_head_problem(p):
            # Turns export only the committed HEAD tree (trusted_turn), so run_agent.py must be
            # in HEAD, not just on disk.
            checks.append(Check(f"runtime:{key}", MISMATCH,
                                f"{key} = {raw} cannot be snapshotted: "
                                f"{runtime_detect.source_head_problem(p)}",
                                "commit run_agent.py in the hermes-agent checkout (every turn "
                                "snapshots HEAD, never the working tree)"))
        elif key == "venv" and runtime_detect.venv_missing(p):
            checks.append(Check(f"runtime:{key}", MISMATCH,
                                f"{key} = {raw} exists but has no "
                                f"{', '.join(runtime_detect.venv_missing(p))}",
                                f"point {key} at the virtualenv Hermes is installed in "
                                "(it must hold bin/python and bin/hermes)"))
        elif key == "rust" and not (p / "bin" / "cargo").exists():
            checks.append(Check(f"runtime:{key}", MISMATCH,
                                f"{key} = {raw} exists but has no bin/cargo",
                                f"point {key} at the Rust toolchain directory"))
        else:
            checks.append(Check(f"runtime:{key}", VERIFIED, f"{key} = {raw}"))
    checks.append(check_node())
    return checks


def check_node() -> Check:
    """#685: the host Node the sandbox mounts at /opt/node, with its version.

    Optional, so an absent Node is a reported skip, never a failure: a seat then has no ``node``
    and JavaScript suites stay unverified, as before.
    """
    from .. import runtime_detect
    root, version = runtime_detect.node_version()
    if root is None:
        return Check("runtime:node", SKIPPED,
                     f"skipped: {version}; seats cannot run `node` and JavaScript test suites "
                     "stay unverified",
                     "install Node under the operator's home (e.g. ~/.hermes/node) or on PATH")
    where = ("mounted at /opt/node" if runtime_detect.node_mounted(root)
             else "system install, already visible in the sandbox")
    return Check("runtime:node", VERIFIED, f"node {version} at {root}, {where}")


def check_scripts() -> Check:
    missing = [name for name in PLUGIN_SCRIPTS if not (scripts_dir() / name).exists()]
    if missing:
        return Check("scripts", ABSENT, "missing from the plugin: " + ", ".join(missing),
                     "reinstall the plugin: the routes and the cron shim run these files by name, "
                     "so a missing one is a seat that can never be woken")
    # Counted from the tuple the check just verified, so the line cannot drift from it.
    gates = [name for name in PLUGIN_SCRIPTS if name.startswith("gate_")]
    others = [name.removesuffix(".py") for name in PLUGIN_SCRIPTS if name not in gates]
    return Check("scripts", VERIFIED,
                 f"{scripts_dir()} ({len(PLUGIN_SCRIPTS)} files: "
                 f"{', '.join(others[:1] + [f'{len(gates)} gates'] + others[1:])})")


def gate_timeout_profiles(loop: dict) -> dict[str, list[str]]:
    """Every profile whose gateway runs one of this loop's route scripts → the routes it hosts."""
    hosted: dict[str, list[str]] = {}
    for role in ("reviewer", "fixer", "adjudicator"):
        if role == "adjudicator" and not str((loop.get("adjudicator") or {}).get("route") or ""):
            continue
        profile = config.seat_profile(loop, role)
        if profile:
            hosted.setdefault(profile, []).append(role)
    if config.triage_enabled(loop):      # gate_triage.py fits its budget to this gateway too
        hosted.setdefault(config.seat_profile(loop, "triage") or "default", []).append("triage")
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
    from .. import gate_failures as gf
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


RATE_LIMIT_MIN = 60     # a burst the loop should absorb: 55 issues filed in a minute (#849)
RATE_LIMIT_SET = 120    # what the fix line asks for


def check_gateway_rate_limit(loop: dict) -> Check:
    """Can each gateway that serves this loop's routes absorb a burst of deliveries (#849)?

    The webhook gateway answers HTTP 429 to the deliveries over ``rate_limit`` per route per
    minute (30 unless set) and GitHub never redelivers, so a burst — 55 issues filed in a minute,
    a rebase of stacked PRs — loses events. The limit is the gateway's one ``rate_limit`` setting
    (``platforms.webhook.extra``), not a key on a route entry, so only ``config.yaml`` can raise
    it, and ``setup``/``arm`` cannot. The watchdog's triage sweep (``triage_sweep``) re-queues an
    issue whose delivery was dropped; a dropped review or fix wake has no such backstop yet.
    """
    from .. import gate_failures as gf
    hosts: dict[pathlib.Path, str] = {}
    for profile in gate_timeout_profiles(loop):
        try:
            for label, host in gf.gateway_hosts(config.profile_dir(profile)):
                hosts.setdefault(host, label)
        except Exception as exc:  # noqa: BLE001 - one unreadable profile must not stop doctor
            return Check("gate:rate_limit", UNKNOWN,
                         f"the gateways serving this loop could not be worked out: "
                         f"{type(exc).__name__}: {exc}")
    rows = []
    for host, label in hosts.items():
        limit, where = gf.gateway_rate_limit(host)
        rows.append((label, host, limit, where))
    unread = [row for row in rows if row[2] is None]
    if unread:
        return Check("gate:rate_limit", UNKNOWN,
                     "cannot read every gateway's rate_limit — "
                     + "; ".join(f"{label}: {where}" for label, _h, _l, where in unread))
    seen = "; ".join(f"{label}: {limit}/min ({where})" for label, _h, limit, where in rows)
    low = [row for row in rows if row[2] < RATE_LIMIT_MIN]
    if low:
        fixes = "; ".join(f"set platforms.webhook.extra.rate_limit: {RATE_LIMIT_SET} in "
                          f"{host / 'config.yaml'} for the {label}" for label, host, _l, _w in low)
        return Check("gate:rate_limit", UNKNOWN,
                     f"{seen} — below {RATE_LIMIT_MIN} deliveries per route per minute, so a "
                     "burst (55 issues filed in a minute, a rebase of stacked PRs) is answered "
                     "HTTP 429 and GitHub does not redeliver those events",
                     f"{fixes}; then restart that gateway")
    return Check("gate:rate_limit", VERIFIED, f"{seen}; a burst of {RATE_LIMIT_MIN} fits")


def check_clone(loop: dict) -> Check:
    """The clone the runs isolate from — and the one rail that would delete it if it were wrong."""
    raw = str(loop.get("clone") or "")
    if not raw:
        if config.isolated_runtime_configured():
            return Check("clone", VERIFIED, "none configured — every turn runs in its own "
                                            "exact-head export, so concurrency above 1 needs no clone")
        return Check("clone", VERIFIED, "none configured — runs work in the checkout they find "
                                        "(fine at concurrency 1)")
    path = pathlib.Path(raw).expanduser()
    if not path.exists():
        return Check("clone", ABSENT, f"no clone at {path}",
                     f"clone it, or `hermes dk set --loop {loop['id']} --clone <path>`: "
                     f"the cleanup prunes worktrees through this path")
    if not path.is_dir() or not (path / ".git").exists():
        return Check("clone", MISMATCH, f"{path} is not a git checkout",
                     f"point `hermes dk set --loop {loop['id']} --clone` at the "
                     f"repository's working clone: isolation clones from it and cleanup prunes "
                     f"worktrees in it")
    # The tree the cleanup deletes, derived from config rather than re-spelled here — a clone that
    # lives inside it is a working copy the cleanup would take with the artifacts.
    root = config.artifacts_dir(loop, 1).parent
    # Same check, same fail-closed rule as isolation.ensure: a path that cannot be resolved is
    # not "outside" — the rail cannot be shown to hold, so this is a failure, not a green.
    try:
        inside = isolation.clone_inside_root(path, root)
    except Exception as exc:
        return Check("clone", MISMATCH, f"cannot tell whether {path} is inside the loop's "
                                        f"artifacts root {root} ({exc})",
                     "fix the unresolvable path (a symlink loop or a permission error): isolation "
                     "refuses to run while it cannot tell, and the cleanup deletes everything "
                     "under artifacts/")
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
    from .. import contained
    per_turn = contained.CHECKOUT_SIZE + contained.SCRATCH_SIZE
    turns = (config.seat_concurrency(loop, "reviewer")
             + config.seat_concurrency(loop, "fixer") + 1)
    worst = per_turn * turns
    detail = (f"/work {_gib(contained.CHECKOUT_SIZE)} + /tmp {_gib(contained.SCRATCH_SIZE)}"
              f" = {_gib(per_turn)} per turn, {_gib(worst)} at {turns} concurrent turns")
    refused_overrides = contained.live_ignored_overrides()
    if refused_overrides:
        refused = ", ".join(f"DIAKTOROS_{name}_GIB={raw!r} ({why})"
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
                     "lower the caps (DIAKTOROS_CHECKOUT_SIZE_GIB / "
                     "DIAKTOROS_SCRATCH_SIZE_GIB), add memory, or let the seats run one at a "
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


def check_fix_ci(loop: dict) -> Check:
    """Whether a failed required check on a fixer's PR becomes a fixer turn (#526). On needs
    unattended fixer pushes, since the CI-fix turn publishes a push."""
    lid = shlex.quote(str(loop.get("id") or "<id>"))
    if loop.get("fix_ci") is not True:
        return Check("fix-ci", VERIFIED,
                     f"off — a red check is only reported (`hermes dk set --loop {lid} "
                     "--fix-ci on` to hand it to the fixer)")
    if not config.unattended_fixer_push_enabled(loop):
        return Check("fix-ci", MISMATCH,
                     "on, but unattended fixer pushes are off, so no CI-fix turn can push",
                     f"`{config.fixer_push_enable_command(loop)}`, or `hermes dk set --loop {lid} "
                     "--fix-ci off`")
    cap = config.ci_fix_cap(loop)
    return Check("fix-ci", VERIFIED,
                 f"on — red required CI on a fixer's PR goes to the fixer first, up to {cap} "
                 "CI-fix turn(s) per PR, spending no review verdict; then the reviewer reviews it "
                 f"(`hermes dk set --loop {lid} --ci-fix-cap N` to change, `--fix-ci off` to stop)")


def base_requires_up_to_date(loop: dict) -> tuple[bool | None, str]:
    """Whether the base requires branches to be up to date before merging (the setting that makes
    GitHub report a PR ``behind``), read from its rulesets (plain read access) and its classic
    branch protection (admin access). ``(True, "")``, ``(False, "")``, or ``(None, why)`` when
    neither answer settles it."""
    base = str(loop.get("base") or "main")
    unread = []
    rules = gh.request(loop, f"/repos/{loop['repo']}/rules/branches/{base}?per_page=100")
    if rules.status != 404 and (rules.error or not isinstance(rules.data, list)):
        unread.append(f"rulesets: {gh.one_line(rules.error or 'no rule list')}")
    for rule in rules.data if isinstance(rules.data, list) else ():
        params = rule.get("parameters") if isinstance(rule, dict) else None
        if (isinstance(rule, dict) and rule.get("type") == "required_status_checks"
                and isinstance(params, dict)
                and params.get("strict_required_status_checks_policy") is True):
            return True, ""
    classic = gh.request(loop, f"/repos/{loop['repo']}/branches/{base}/protection/required_status_checks")
    if classic.status != 404 and (classic.error or not isinstance(classic.data, dict)):
        unread.append(f"classic branch protection: {gh.one_line(classic.error or 'no answer')}")
    elif isinstance(classic.data, dict) and classic.data.get("strict") is True:
        return True, ""
    return (None, "; ".join(unread)) if unread else (False, "")


def check_review_only_update(loop: dict, offline: bool = False) -> Check | None:
    """The host's merge pushes to a review-only author's branch (a branch the loop does not own):
    off is the default; on, the pushing token must be able to push to the repository."""
    lid = shlex.quote(str(loop.get("id") or "<id>"))
    if not config.review_only_update(loop):
        return None
    if not config.unattended_fixer_push_enabled(loop):
        return Check("review-only-update", MISMATCH,
                     "on, but unattended fixer pushes are off, so the host pushes nothing",
                     f"`{config.fixer_push_enable_command(loop)}`, or `hermes dk set --loop {lid} "
                     "--review-only-update off`")
    login = config.seat_login(loop, "fixer")
    if offline:
        return Check("review-only-update", UNKNOWN,
                     f"on — {login} pushes merge commits to review-only branches; the token's push "
                     "access was not probed (--offline)")
    repo = gh.api(loop, f"/repos/{loop['repo']}", login=login)
    perms = repo.get("permissions") if isinstance(repo, dict) else None
    if not isinstance(perms, dict):
        return Check("review-only-update", UNKNOWN,
                     f"on — could not read {login}'s access to {loop['repo']}",
                     f"check {login}'s token can read {loop['repo']}")
    if perms.get("push") is not True:
        return Check("review-only-update", MISMATCH,
                     f"on, but {login}'s token cannot push to {loop['repo']}: the host's merge "
                     "push would be refused",
                     f"give {login} write access to {loop['repo']}, or `hermes dk set --loop {lid} "
                     "--review-only-update off`")
    strict, unread = base_requires_up_to_date(loop)
    if strict is not True:
        # Not a failure: the notice still goes out. Only the push can never fire (#504).
        why = (f"{loop['base']} does not require branches to be up to date"
               if strict is False else
               f"could not read whether {loop['base']} requires branches to be up to date "
               f"({unread})")
        return Check("review-only-update", UNKNOWN,
                     f"on — {login} can push to {loop['repo']}, but {why}: GitHub then never "
                     "reports a PR as `behind`, a conflicting PR cannot be pushed, so the host "
                     "pushes nothing and only the conflict notice applies",
                     f"to let it push, require branches to be up to date before merging on "
                     f"{loop['base']} (branch protection or a ruleset: 'strict required status "
                     "checks')")
    return Check("review-only-update", VERIFIED,
                 f"on — {login} can push to {loop['repo']}; clean merges of {loop['base']} are "
                 "pushed to same-repository review-only branches under a lease when GitHub "
                 f"reports the PR `behind` ({loop['base']} requires up-to-date branches), never "
                 "conflicts, workflow changes or forks")


def check_attribution(loop: dict) -> Check:
    """Whether what the loop posts is signed (#197). Both answers are valid; this only says which."""
    from .. import attribution
    lid = shlex.quote(str(loop.get("id") or "<id>"))
    if attribution.enabled(loop):
        return Check("attribution", VERIFIED,
                     "on — reviews, comments and commits the loop posts are signed 'Automated by "
                     f"Diaktoros' (`hermes dk set --loop {lid} --attribution off` "
                     "to stop)")
    return Check("attribution", VERIFIED,
                 "off — what the loop posts carries no 'Automated by Diaktoros' footer or "
                 f"trailer (`hermes dk set --loop {lid} --attribution on` to sign it)")


def check_authorship(loop: dict, offline: bool = False) -> Check:
    """Commits on open loop PRs that show as a seat's account but that no seat turn pushed (#757).
    Report-only: a finding is a warning (``unknown``), never a failure, and blocks nothing."""
    from .. import authorship
    name = "authorship"
    agents = sorted(config.operator_agents(loop))
    named = (f" · operator agents (post as the operator's account): {', '.join(agents)}"
             if agents else "")
    if offline:
        return Check(name, UNKNOWN, "not probed (--offline): PR commits not read" + named)
    prs, error = gh.open_prs_read(loop)
    if prs is None:
        return Check(name, UNKNOWN, f"open PRs unreadable ({error}): authorship not checked")
    pushed, error = authorship.pushed_heads(loop)
    if error:
        return Check(name, UNKNOWN, f"push journal unreadable ({error}): authorship not checked")
    found, unread = [], []
    numbers = [pr.get("number") for pr in prs if isinstance(pr.get("number"), int)]
    for number in numbers[:authorship.PRS_MAX]:
        stray, problem = authorship.scan(loop, number, pushed)
        if problem:
            unread.append(f"#{number}: {problem}")
        found += [f"#{number} {authorship.line(entry)}" for entry in stray]
    if found:
        return Check(name, UNKNOWN, "misattributed: " + "; ".join(found[:5])
                     + (f" (+{len(found) - 5} more)" if len(found) > 5 else "") + named,
                     "give each agent its own GitHub account and git email (docs/accounts.md); "
                     "this check never blocks a turn")
    if unread or len(numbers) > authorship.PRS_MAX:
        return Check(name, UNKNOWN, "no misattributed commit found, but "
                     + ("; ".join(unread[:3]) if unread else
                        f"only the first {authorship.PRS_MAX} of {len(numbers)} open PRs were read")
                     + named)
    return Check(name, VERIFIED, f"no commit on {len(numbers)} open PR(s) shows as a seat's "
                 f"account without a seat turn pushing it" + named)


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
                     "fix them (edit \"roots\" in the loop file, then run `hermes dk apply --loop <id>`): the cleanup only ever deletes "
                     "inside a configured root")
    return Check("roots", VERIFIED, f"{len(roots)} configured: " + ", ".join(roots))


def check_gateway_scripts(loop: dict) -> list[Check]:
    """Each installed route's script, resolved exactly as the gateway resolves it (issue #105):
    under the serving profile's ``scripts/``, a real file inside it, and this plugin's shim."""
    from .. import gate_shims
    status_of = {"ok": VERIFIED, "absent": ABSENT, "mismatch": MISMATCH}
    return [Check(name, status_of[status], detail, fix)
            for name, status, detail, fix in gate_shims.live_checks(loop)]


def check_host_names() -> Check:
    """Whether this install still keeps host files under their names from before the rename
    (#425): they work where they are, and ``migrate`` moves them."""
    old = [config.HOST_FILES[key][1] for key in config.HOST_FILES
           if (config.home() / config.HOST_FILES[key][1]).exists()]
    # The ledger's side files (its "a ledger existed" marker, the worker log) carry its old name
    # too, and are left behind if only the ledger moved.
    from ..migrate import ledger_sides
    ledger_old = config.home() / config.HOST_FILES["ledger"][1]
    old += [str(side.relative_to(config.home())) for side in ledger_sides(ledger_old)]
    if not old:
        return Check("host-names", VERIFIED, "every host file has its new name")
    return Check("host-names", UNKNOWN,
                 f"{len(old)} host file(s) still have their names from before the rename: "
                 f"{', '.join(old)} — they work where they are",
                 "between turns, run `hermes dk migrate --dry-run`, then `hermes dk migrate`")


def _read_list(loop: dict, path: str) -> tuple[list | None, str]:
    items: list = []
    for page in range(1, 101):
        response = gh.request(loop, f"{path}?per_page=100&page={page}")
        if response.error or not isinstance(response.data, list):
            return None, response.error or "not a list"
        items.extend(response.data)
        if len(response.data) < 100:
            return items, ""
    return None, "listing too long"


def check_labels(loop: dict, offline: bool) -> Check | None:
    """Every label in the triage allowlist exists on the repo (None when triage is off)."""
    if not config.triage_enabled(loop):
        return None
    wanted = list((loop.get("triage") or {}).get("labels") or [])
    if offline:
        return Check("labels", UNKNOWN, "not probed (--offline)")
    found, error = _read_list(loop, f"/repos/{loop['repo']}/labels")
    if found is None:
        return Check("labels", UNKNOWN, f"could not read the repo's labels: {gh.one_line(error)}",
                     f"check by hand with `gh api repos/{loop['repo']}/labels`")
    have = {str(x.get("name")).casefold() for x in found if isinstance(x, dict)}
    missing = [x for x in wanted if str(x).casefold() not in have]
    if missing:
        return Check("labels", MISMATCH, f"triage labels missing on {loop['repo']}: {', '.join(missing)}",
                     "create them: " + "; ".join(f"`gh label create {m!r} -R {loop['repo']}`"
                                                 for m in missing))
    return Check("labels", VERIFIED, f"all {len(wanted)} triage labels exist")


def check_branch_protection(loop: dict, offline: bool) -> Check | None:
    """The base's required status checks against the loop's ``required_checks``.

    Only a mismatch (or an unreadable answer) is reported; None when the loop names no checks
    or they agree. A warning, not a failure: the loop runs, it just gates differently than GitHub.
    """
    wanted = config.required_checks(loop)
    if not wanted:
        return None
    if offline:
        return Check("branch-protection", UNKNOWN, "not probed (--offline)")
    base = str(loop.get("base") or "main")
    # GitHub requires checks two ways: classic branch protection (its read needs admin access)
    # and rulesets (readable with plain read access). A base protected only by a ruleset answers
    # "Branch not protected" to the classic read, so both are read and their checks merged.
    have: set[str] = set()
    unread = []
    rules = gh.request(loop, f"/repos/{loop['repo']}/rules/branches/{base}?per_page=100")
    if rules.status != 404 and (rules.error or not isinstance(rules.data, list)):
        unread.append(f"rulesets ({gh.one_line(rules.error or 'no rule list')})")
    elif isinstance(rules.data, list):
        for rule in rules.data:
            if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
                params = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
                have |= {str(c.get("context")) for c in params.get("required_status_checks") or []
                         if isinstance(c, dict) and c.get("context")}
    classic = gh.request(loop, f"/repos/{loop['repo']}/branches/{base}/protection/required_status_checks")
    if classic.status != 404 and (classic.error or not isinstance(classic.data, dict)):
        unread.append(f"classic branch protection ({gh.one_line(classic.error or 'no answer')})")
    elif isinstance(classic.data, dict):
        have |= {str(c) for c in classic.data.get("contexts") or []}
        have |= {str(c.get("context")) for c in classic.data.get("checks") or []
                 if isinstance(c, dict)}
    have.discard(ci.MERGE_CHECK)       # the loop's own check: never a CI check (merge-check, #1163)
    if len(unread) == 2:
        return Check("branch-protection", UNKNOWN,
                     f"could not read the required checks of {base}: {'; '.join(unread)}",
                     f"{config.reader_access_fix(loop, 'protection')}, or compare by hand in "
                     "repo settings → rules and branches")
    want = set(wanted)
    if want == have:
        return None
    parts = []
    if want - have:
        parts.append(f"loop requires but {base} does not: {', '.join(sorted(want - have))}")
    if have - want:
        parts.append(f"{base} requires but the loop does not: {', '.join(sorted(have - want))}")
    if unread:
        parts.append(f"not read: {unread[0]}")
    return Check("branch-protection", UNKNOWN, "; ".join(parts),
                 f"align `required_checks` with the required status checks of {base}")


def check_merge_check(loop: dict, offline: bool) -> Check | None:
    """The base's protection against the ``diaktoros/review`` merge check (#1163 M2c): required
    (in App mode pinned to the reviewer App) when the loop posts it, never required when it does
    not, and no required approvals in App mode (GitHub never counts the App's). None when the loop
    does not post the check and nothing requires it."""
    from .. import protection
    lid = shlex.quote(str(loop.get("id") or "<id>"))
    posting = config.merge_check_on(loop)
    if offline:
        return (Check("merge-check", UNKNOWN, "not probed (--offline)") if posting else None)
    found = protection.read(loop)
    if not found.known:
        if not posting:
            return None
        return Check("merge-check", UNKNOWN,
                     f"could not read {loop['base']}'s protection: {'; '.join(found.unread)}",
                     f"{config.reader_access_fix(loop, 'protection')}, or check by hand that "
                     f"{loop['base']} requires {ci.MERGE_CHECK} (repo settings → rules)")
    blocking, warnings, facts = protection.problems(loop, found)
    if not (blocking or warnings or facts):
        return None
    protect = f"hermes dk protect --loop {lid} --admin-token LOGIN"
    require = f"`{protect}`"
    if blocking:
        fix = (f"`hermes dk set --loop {lid} --merge-check on`, or `{protect} --remove` (it "
               "removes only the loop's own ruleset)" if not posting else
               f"{require} (it rewrites the loop's own ruleset for this reviewer), and remove "
               "any other requirement of the check in repo settings → rules")
        return Check("merge-check", MISMATCH, "; ".join(blocking + warnings + facts), fix)
    if warnings:
        fixes = []
        if any("approving review" in w for w in warnings):
            fixes.append(f"turn required approvals off on {loop['base']} (repo settings → rules), "
                         "or keep them and approve every PR yourself")
        if any(ci.MERGE_CHECK in w and "approving" not in w for w in warnings):
            fixes.append(require)
        if any(w.startswith("could not read") for w in warnings):
            fixes.append(f"{config.reader_access_fix(loop, 'protection')}, or check "
                         f"{loop['base']} by hand in repo settings → rules")
        return Check("merge-check", UNKNOWN, "; ".join(warnings + facts), "; ".join(fixes))
    return Check("merge-check", VERIFIED, "; ".join(facts))


UPSTREAM_REPO = "coe0718/diaktoros"


def installed_commit() -> str:
    """The installed plugin's commit (its git checkout's HEAD), or "" when it is not a checkout."""
    try:
        proc = subprocess.run(["git", "-C", str(plugin_root()), "rev-parse", "HEAD"],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else ""


class PluginComparison(NamedTuple):
    """GitHub's compare of the installed commit with a target (#904): the one reading both
    doctor's ``plugin-version`` and ``hermes dk update`` take, so they cannot disagree."""
    status: str | None = None       # ahead / behind / identical / diverged (the target's side)
    ahead_by: int | None = None     # commits the target has that the installed commit lacks
    commits: tuple = ()             # those commits, oldest first (GitHub lists at most 250)
    error: str = ""


def compare_plugin(loop: dict, sha: str, target: str = "main") -> PluginComparison:
    """``sha`` (installed) compared with ``target`` (a branch, tag or sha) in the upstream repo."""
    resp = gh.request(loop, f"/repos/{UPSTREAM_REPO}/compare/{sha}...{target}")
    data = resp.data if isinstance(resp.data, dict) else {}
    commits = data.get("commits")
    commits = tuple(c for c in commits if isinstance(c, dict)) if isinstance(commits, list) else ()
    return PluginComparison(data.get("status") if isinstance(data.get("status"), str) else None,
                            data.get("ahead_by"), commits, resp.error)


def check_plugin_version(loop: dict, offline: bool = False) -> Check:
    """#556: is the installed plugin behind the latest release or ``main``?

    With --offline it is reported as not probed (``unknown``), never left out of the report.

    Behind the latest release is a warning (``unknown``), never a failure. Either side
    unreadable is ``unknown`` too. Only a git checkout can be compared; a catalog install
    without ``.git`` reports unknown.
    """
    name = "plugin-version"
    if offline:
        return Check(name, UNKNOWN, "not probed (--offline)")
    sha = installed_commit()
    if not sha:
        return Check(name, UNKNOWN, "could not read the installed plugin's commit (not a git checkout)",
                     f"compare by hand with the latest release of {UPSTREAM_REPO}")
    short = sha[:7]
    base = f"/repos/{UPSTREAM_REPO}"
    release = gh.request(loop, f"{base}/releases/latest")
    tag = str(release.data.get("tag_name") or "") if isinstance(release.data, dict) else ""
    if release.error or not tag:
        return Check(name, UNKNOWN, f"could not read the latest release of {UPSTREAM_REPO}: "
                     f"{gh.one_line(release.error or 'no tag')}")
    since = gh.request(loop, f"{base}/compare/{tag}...{sha}")
    status = since.data.get("status") if isinstance(since.data, dict) else None
    main = compare_plugin(loop, sha, "main")
    behind_main = main.ahead_by
    if since.error or not status or main.error or type(behind_main) is not int:
        return Check(name, UNKNOWN, f"could not compare installed {short} with {tag} or main: "
                     f"{gh.one_line(since.error or main.error or 'no comparison')}")
    commits = f"{behind_main} commit{'s' if behind_main != 1 else ''} behind main"
    if status in ("ahead", "identical"):
        return Check(name, VERIFIED, f"installed {short} is at or after the latest release ({tag}); "
                     f"{commits}")
    return Check(name, UNKNOWN, f"installed {short} is {commits} and 1 release behind ({tag})",
                 "update the installed plugin to the latest release")


def check_environment(loop: dict, offline: bool) -> list[Check]:
    """The GitHub-side contract: token expiry and scopes, triage labels, branch protection."""
    from . import identity_checks   # here, not module level (#676)
    checks = identity_checks.check_token_expiry_scopes(loop, offline)
    for extra in (check_labels(loop, offline), check_branch_protection(loop, offline),
                  check_merge_check(loop, offline)):
        if extra:
            checks.append(extra)
    return checks


def check_loop(loop: dict, offline: bool = False) -> list[Check]:
    """Every check, in the order an operator reads an install: what it is, who runs it, what
    wakes it, what schedules it, and where it works."""
    from . import (cron_checks, hook_checks, identity_checks,   # here, not module level (#676)
                   route_checks, seat_checks)
    checks = [check_config(loop), check_turn_budget(loop), check_host_names()]
    duplicate = check_duplicate_repo(loop)
    if duplicate:
        checks.append(duplicate)
    for seat in ("reviewer", "fixer"):
        checks.append(seat_checks.check_profile(loop, seat))
        checks.append(identity_checks.check_credential(loop, seat))
    if str((loop.get("adjudicator") or {}).get("route") or ""):
        checks.append(seat_checks.check_adjudicator_profile(loop))
    if config.triage_enabled(loop):
        checks.extend(seat_checks.check_triage(loop))
    checks.extend(seat_checks.check_seat_models(loop))
    checks.extend(seat_checks.check_seat_extras(loop))
    checks.append(check_fixer_push(loop))
    checks.extend(filter(None, [check_review_only_update(loop, offline)]))
    checks.append(check_fix_ci(loop))
    checks.append(check_attribution(loop))
    checks.append(check_authorship(loop, offline))
    checks.append(check_sandbox_caps(loop))
    checks.extend(filter(None, [identity_checks.check_adjudicator_identity(loop)]))
    checks.extend(identity_checks.check_tokens(loop))
    checks.extend(identity_checks.check_app_live(loop, login, offline) for login in sorted(loop.get("apps") or {}))
    checks.append(identity_checks.check_read_token(loop))
    checks.extend(route_checks.check_routes(loop))
    checks.extend(check_gateway_scripts(loop))
    checks.append(check_scripts())
    checks.extend(check_gate_timeouts(loop))
    checks.append(check_gateway_rate_limit(loop))
    checks.append(cron_checks.check_shim(loop))
    checks.append(cron_checks.check_cron_job(loop))
    checks.append(cron_checks.check_watchdog_last_run(loop))
    checks.extend(check_runtime_paths(loop))
    checks.append(check_clone(loop))
    checks.append(check_state_dir(loop))
    checks.append(check_roots(loop))
    checks.append(hook_checks.check_gateway(loop, offline))
    checks.extend(hook_checks.check_hooks(loop, offline))
    checks.extend(check_environment(loop, offline))
    checks.append(check_plugin_version(loop, offline))
    return checks


def report(loop: dict, checks: list[Check], strict: bool = False) -> int:
    """Print one loop's preflight. Returns 1 when something has to be fixed, else 0."""
    from . import hook_checks   # here, not module level (#676)
    failed = [check for check in checks if check.failed]
    unknown = [check for check in checks if check.status == UNKNOWN]
    verified = [check for check in checks if check.status == VERIFIED]

    print()
    print(f"[{loop['id']}] {loop['repo']} — preflight "
          f"(read-only: it writes nothing and fires nothing)")
    for check in checks:
        print(f"  {MARKS[check.status]} {check.name:<20} {hook_checks._safe_report_text(check.detail)}")
        if check.failed and check.fix:
            print(f"      fix: {hook_checks._safe_report_text(check.fix)}")
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
              f"`hermes dk arm --loop {loop['id']}`.")
    if strict and unknown and not failed:
        print(f"  --strict: {len(unknown)} undecided check(s) count as a failure.")
    return 1 if failed or (strict and unknown) else 0
