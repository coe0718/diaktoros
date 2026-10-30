"""The watchdog-schedule checks (#1349, split from the doctor core): the cron job that runs the
watchdog exists, runs the shim this plugin installs on its schedule and delivers where it should
(``check_cron_job``), the shim itself (``check_shim``), and when the watchdog last ran
(``check_watchdog_last_run``). Read-only, like every doctor check. Each reaches the core through
``doctor`` (``from .. import doctor``); callers use ``cron_checks.<name>``.
"""
from __future__ import annotations

import json
import pathlib
import re
from datetime import datetime

from .. import config, doctor


def check_watchdog_last_run(loop: dict) -> doctor.Check:
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
    from .. import state as state_mod
    from ..util import epoch
    st = state_mod.state_for(loop)
    raw = st.watch().get("last_run")
    if not raw:
        return doctor.Check("watchdog:run", doctor.UNKNOWN,
                            "no last_run recorded in watchdog.json — the watchdog may not have "
                            "run yet (or its state is unreadable)",
                            f"run `hermes dk watchdog --loop {loop['id']}` once, or wait "
                            "for the next scheduled run; if it never stamps last_run, the shim or "
                            "the scheduler is broken (see cron:shim / cron:job)")
    last = epoch(raw)
    if not last:
        return doctor.Check("watchdog:run", doctor.UNKNOWN,
                            f"last_run in watchdog.json is not a parseable timestamp ({raw!r})",
                            "check watchdog.json — a corrupt stamp means the watchdog state needs "
                            "repair before staleness can be judged")
    schedule_min = _watchdog_schedule_minutes(loop)
    if schedule_min is None:
        return doctor.Check("watchdog:run", doctor.UNKNOWN,
                            f"last run {raw} ({(_time.time() - last) / 60:.0f}m ago), but the "
                            "schedule interval could not be read from the cron store",
                            "check `hermes cron list` or the cron store; the staleness threshold "
                            "is 2× the schedule interval")
    threshold_s = 2 * schedule_min * 60
    age_s = _time.time() - last
    if age_s > threshold_s:
        return doctor.Check("watchdog:run", doctor.MISMATCH,
                            f"last run {raw} ({age_s / 60:.0f}m ago) is older than 2× the "
                            f"{schedule_min}m schedule ({threshold_s / 60:.0f}m) — the watchdog "
                            "has stopped running",
                            f"check the cron job (`hermes cron list`), the shim (`{doctor.shim_path()}`) "
                            f"and the watchdog log ({st.log}); resume or replace the job with "
                            f"{doctor.cron_fix(loop)}")
    return doctor.Check("watchdog:run", doctor.VERIFIED,
                        f"last run {raw} ({age_s / 60:.0f}m ago, within 2× the {schedule_min}m "
                        f"schedule = {threshold_s / 60:.0f}m)")


def _watchdog_schedule_minutes(loop: dict) -> int | None:
    """The watchdog job's schedule interval in minutes, or None if it cannot be read."""
    path = doctor.cron_store()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return None
    wanted = doctor.watchdog_job_name(loop)
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


_WATCHDOG_LINE = re.compile(r"WATCHDOG\s*=\s*pathlib\.Path\((['\"])(?P<path>.+?)\1\)")


def check_shim(loop: dict) -> doctor.Check:
    """The cron shim, and the plugin path it is pinned to.

    The shim is written once, at init, with the *absolute* path of the watchdog that existed
    then. An upgrade that moves the plugin directory leaves a shim pointing at a path that no
    longer runs this code — the scheduler keeps firing it, and the loop keeps looking armed.
    """
    path = doctor.shim_path()
    if not path.exists():
        return doctor.Check("cron:shim", doctor.ABSENT, f"no {path}",
                            f"{doctor.shim_fix(loop)}; then, if no job runs it yet: {doctor.cron_fix(loop)}")
    try:
        text = path.read_text()
    except Exception as exc:
        return doctor.Check("cron:shim", doctor.MISMATCH, f"{path} cannot be read ({exc})",
                            f"chmod +r {path}, or {doctor.shim_fix(loop)}")
    # Do not run arbitrary installed shim code during a read-only preflight. Instead require
    # byte-for-byte identity with the shim init actually generates; a dead assignment, commented
    # line or altered subprocess invocation cannot pass merely by mentioning WATCHDOG.
    live = str(doctor.scripts_dir() / "watchdog.py")
    from .. import cli
    expected = cli.SHIM.format(watchdog=pathlib.Path(live))
    if text != expected:
        return doctor.Check("cron:shim", doctor.MISMATCH, f"{path} differs from init's executable shim for {live}",
                            doctor.shim_fix(loop))
    pinned = live
    return doctor.Check("cron:shim", doctor.VERIFIED, f"{path} → {pinned}")


def check_cron_job(loop: dict) -> doctor.Check:
    """The scheduled job itself, read from the scheduler's own store (no scheduler needed)."""
    path = doctor.cron_store()
    if not path.exists():
        return doctor.Check("cron:job", doctor.ABSENT, f"no cron store at {path}",
                            doctor.cron_fix(loop))
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return doctor.Check("cron:job", doctor.MISMATCH, f"{path} is not readable JSON ({exc})",
                            "repair the job store (a store the scheduler cannot read is a watchdog that "
                            f"never sweeps), then, if the job is gone: {doctor.cron_fix(loop)}")
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return doctor.Check("cron:job", doctor.MISMATCH, f"{path} has no job list",
                            f"repair the job store, then, if the job is gone: {doctor.cron_fix(loop)}")
    wanted = doctor.watchdog_job_name(loop)
    # A pre-#60 install has one per-loop job per loop; each sweeps every loop, so N jobs give
    # N sweeps per tick (#60). Any shim job beyond the single shared one is a duplicate sweep:
    # name them all for migration — remove the extras, keep (or create) one shared job.
    shared = [entry for entry in jobs if isinstance(entry, dict)
              and str(entry.get("name") or "").strip() == wanted]
    shim_jobs = [entry for entry in jobs if isinstance(entry, dict)
                 and pathlib.Path(str(entry.get("script") or "")).name == doctor.shim_path().name]
    if not shared and shim_jobs:
        ids = ", ".join(str(entry.get("id") or "?") for entry in shim_jobs)
        fix = doctor.migration_fix(loop, shim_jobs)
        try:
            loop_count = len(config.all_loops())
        except Exception:
            loop_count = 2
        if len(shim_jobs) == 1 and loop_count <= 1:
            # 1 job × 1 loop = 1 sweep: no N² cost yet, so not a blocker.
            return doctor.Check("cron:job", doctor.UNKNOWN,
                                f"1 per-loop watchdog job ({ids}) — fine for one loop; migrate to the "
                                "one shared job when you add a second loop", fix)
        return doctor.Check("cron:job", doctor.MISMATCH,
                            f"{len(shim_jobs)} per-loop watchdog job(s) ({ids}) each sweep every loop "
                            f"(N jobs × N loops = N² sweeps per tick) — migrate to the one shared job",
                            fix)
    job = shared[0] if shared else None
    if job is None:
        # A job the operator wrote by hand: same shim, a name that names the loop.
        job = next((entry for entry in jobs if isinstance(entry, dict)
                    and pathlib.Path(str(entry.get("script") or "")).name == doctor.shim_path().name
                    and (loop["id"] in str(entry.get("name") or "")
                         or str(entry.get("name") or "").strip() == doctor.watchdog_job_name({}))), None)
    if job is None:
        return doctor.Check("cron:job", doctor.ABSENT, f"no job named {wanted!r} in {path}",
                            doctor.cron_fix(loop))
    job_id = doctor._job_ref(job, wanted)
    named = [doctor._job_ref(entry, wanted) for entry in jobs if isinstance(entry, dict)
             and str(entry.get("name") or "").strip() == wanted]
    if len(named) > 1:
        return doctor.Check("cron:job", doctor.MISMATCH,
                            f"{len(named)} jobs are named {wanted!r} ({', '.join(named)}) — each one "
                            "fires the watchdog",
                            doctor.cron_replace_fix(loop, named, doctor._job_deliver(job)))
    # More than one shim job total (a leftover legacy job beside the shared one) is the same
    # N-sweeps-per-tick problem: name the extras so they can be removed.
    if len(shim_jobs) > 1:
        extra = [doctor._job_ref(entry, wanted) for entry in shim_jobs
                 if doctor._job_ref(entry, wanted) != job_id]
        removals = "; ".join(f"`hermes cron remove {extra_id}`" for extra_id in extra)
        return doctor.Check("cron:job", doctor.MISMATCH,
                            f"{len(shim_jobs)} jobs run {doctor.shim_path().name} "
                            f"({', '.join(str(e.get('id') or '?') for e in shim_jobs)}) "
                            "— each sweeps every loop (N² sweeps per tick); keep one",
                            removals)
    replace = doctor.cron_replace_fix(loop, [job_id], doctor._job_deliver(job))
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
        return doctor.Check("cron:job", doctor.MISMATCH, f"{job_id} ({wanted}) is {reason}",
                            fix)
    if job.get("script") != doctor.shim_path().name or job.get("no_agent") is not True:
        return doctor.Check("cron:job", doctor.MISMATCH,
                            f"{job_id} runs {job.get('script')!r} (no_agent={job.get('no_agent')!r}), "
                            f"expected {doctor.shim_path().name!r} with --no-agent", replace)
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
        return doctor.Check("cron:job", doctor.MISMATCH,
                            f"{job_id} has no valid stored schedule (display text does not schedule work)",
                            replace)
    next_run = job.get("next_run_at")
    try:
        if not isinstance(next_run, str) or not next_run.strip():
            raise ValueError("missing next run")
        datetime.fromisoformat(next_run.replace("Z", "+00:00"))
    except ValueError:
        return doctor.Check("cron:job", doctor.MISMATCH,
                            f"{job_id} has no valid next_run_at — cannot verify the next wake "
                            "(the scheduler may recompute a missing value for a recurring job)",
                            replace)
    if missing_croniter:
        return doctor.Check("cron:job", doctor.UNKNOWN,
                            f"{job_id} cron schedule could not be validated here (croniter unavailable); "
                            "the stored job may be valid — check on the scheduler host")
    schedule = (job.get("schedule_display")
                or ((job.get("schedule") or {}).get("display") if isinstance(job.get("schedule"), dict)
                    else "")
                or "?")
    return doctor.Check("cron:job", doctor.VERIFIED, f"{job_id} {schedule}, next {next_run}")
