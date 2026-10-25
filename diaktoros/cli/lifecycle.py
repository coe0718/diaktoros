"""Lifecycle commands (#676): ``migrate``, ``backup``, ``restore`` and ``uninstall``.

Moving an install to a new name or repository, saving and restoring it, and taking a loop (or
the whole install) down: hooks, routes, the watchdog's job and shim, state. ``cli`` re-exports
every command here; the install plumbing they share with ``init`` and ``set`` is
``cli_common``'s, called as ``cli_common.<name>``.
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import shutil
import sqlite3
import subprocess
import sys

from .. import (
    config,
    doctor,
    gate_shims,
    gh,
    route_intent,
    routes,
)
from . import common as cli_common
from .common import SHARED_JOB_NAME, SHARED_JOB_NAMES, SHIM


def watchdog_job_name(loop: dict) -> str:
    """The scheduler job name for a loop — now the shared name; the loop id is not part of it.

    Kept as a function of ``loop`` for the callers that already hold one (and for doctor to
    recognise a legacy per-loop job from before #60). A job created by ``init`` uses
    ``SHARED_JOB_NAME``: one job sweeps every loop.
    """
    return config.watchdog_job_name()


def _hook_find_command(loop: dict) -> str:
    """A pasteable ``gh`` command listing the ids of the hooks posting to this loop's routes."""
    names = "|".join(sorted(cli_common._hook_route_names(loop)))
    jq = f'.[] | select(.config.url | test("/webhooks/({names})/?$")) | .id'
    repo = loop["repo"]
    return (f"gh api {shlex.quote(f'repos/{repo}/hooks?per_page=100')} "
            f"--jq {shlex.quote(jq)}")


def _delete_loop_hooks(loop: dict, login: str | None
                       ) -> tuple[list[str], list[str], list[int], bool]:
    """Delete this loop's repo hooks and read the listing back: ``(done, failures, left, unread)``.

    Deleted rather than paused: a paused hook still signs with a secret the next install's route
    will not hold, and it is exactly what a later ``init --hooks`` would trip over.

    ``unread`` is the structured fact that some DELETEs were accepted but the read-back could not
    confirm them (the honest case): it lets ``_uninstall_refused`` choose that remedy by fact
    rather than by matching the reason's wording (#131).
    """
    hooks, foreign, error = cli_common._classify_hooks(loop, login)
    if hooks is None:
        return [], [f"could not read the repo's hooks: {error}"], [], False
    done, failures = cli_common._foreign_lines(loop, foreign), []
    login = login or loop.get("read_token")
    if not hooks:
        # Nothing of ours to delete — but "gone" is only ever concluded from a read: a second
        # sample still catches a hook created meanwhile (a concurrent arm or init --hooks), and
        # an unreadable one is "not confirmed", never a claim that anything is live.
        after, error = cli_common._loop_hooks(loop, login)
        if after is None:
            return done, [f"could not confirm that no hook of this loop's appeared while uninstall "
                          f"ran (the listing read-back failed: {error}) — nothing was deleted"], [], False
        if after:
            ids = sorted(hook["id"] for hook in after)
            return done, [f"hook{'s' if len(ids) > 1 else ''} {', '.join(map(str, ids))} "
                          "appeared on this loop's route URLs while uninstall ran (a concurrent "
                          "arm or init --hooks?) — not deleted"], ids, False
        return done, failures, [], False
    refused: list[int] = []                       # ids whose DELETE failed, recorded as they fail
    for hook in hooks:
        _, error = gh.fetch(loop, f"/repos/{loop['repo']}/hooks/{hook['id']}", method="DELETE",
                            login=login)
        if error:
            refused.append(hook["id"])
            failures.append(f"hook {hook['id']}: DELETE failed ({error})")
    after, error = cli_common._loop_hooks(loop, login)
    if after is None:
        # Only the hooks whose DELETE failed are known to be live; the rest were accepted and
        # merely not read back — say exactly that, never "still live" about ids that are gone.
        accepted = [hook["id"] for hook in hooks if hook["id"] not in refused]
        if accepted:
            failures.append(f"could not confirm the deletion of hook"
                            f"{'s' if len(accepted) > 1 else ''} "
                            f"{', '.join(map(str, accepted))} (GitHub accepted each DELETE; the "
                            f"listing read-back failed: {error})")
        else:
            failures.append(f"could not confirm the deletion: {error}")
        return done, failures, refused, bool(accepted)
    left = {hook["id"] for hook in after}
    for hook in hooks:
        if hook["id"] not in left:
            done.append(f"hook {hook['id']} deleted")
    return done, failures, sorted(left), False


def _other_loops(loop: dict) -> list[str]:
    """Loop ids configured besides ``loop`` — the shared job must outlive any one loop."""
    directory = config.config_dir()
    if not directory.exists():
        return []
    return [path.stem for path in sorted(directory.glob("*.json"))
            if path.stem != loop.get("id")]


def _remove_cron(loop: dict) -> tuple[list[str], list[str]]:
    """Remove the watchdog job through the scheduler's own CLI, then read the store back.

    The shared job is removed only with the **last** loop (#60): any other loop still needs it.
    A legacy per-loop job belonging to this loop is always removed — it is this loop's own.
    """
    jobs, error = cli_common._cron_jobs(loop)
    if jobs is None:
        return [], [f"cron: {error}"]
    others = _other_loops(loop)
    shared = [job for job in jobs
              if str(job.get("name") or "").strip() in SHARED_JOB_NAMES]
    legacy = [job for job in jobs
              if str(job.get("name") or "").strip() == cli_common._legacy_job_name(loop)]
    removing = legacy + (shared if not others else [])
    keeping = [job for job in jobs if job not in removing]
    hermes = cli_common._hermes_bin()
    done, failures = [], []
    for job in removing:
        job_id = str(job.get("id") or "")
        name = str(job.get("name") or "")
        if not job_id:
            failures.append(f"cron: job {name!r} has no id")
            continue
        if not hermes:
            failures.append(f"cron: no `hermes` on PATH to remove job {job_id}")
            continue
        try:
            proc = subprocess.run([hermes, "cron", "remove", job_id], capture_output=True,
                                  text=True, timeout=120)
        except Exception as exc:
            failures.append(f"cron: removing job {job_id} failed ({exc})")
            continue
        if proc.returncode != 0:
            failures.append(f"cron: removing job {job_id} failed "
                            f"({(proc.stderr or proc.stdout).strip()[:200]})")
    after, error = cli_common._cron_jobs(loop)
    if after is None:
        return done, failures + [f"cron: could not confirm the removal: {error}"]
    left = {str(job.get("id") or "") for job in after}
    for job in removing:
        if str(job.get("id") or "") in left:
            if not failures:
                failures.append(f"cron: job {job.get('id')} is still scheduled")
        else:
            done.append(f"cron job removed: {job.get('id')} ({job.get('name')})")
    for job in keeping:
        done.append(f"cron job kept: {job.get('id')} ({job.get('name')}) — "
                    f"{len(others)} other loop(s) still sweep through it")
    return done, failures


def _remove_unused_shim() -> str:
    """The cron shim is shared by every loop's job: remove it only when no job runs it any more."""
    shim = config.watchdog_shim()
    if not shim.is_symlink() and not shim.is_file():
        return ""
    try:
        data = json.loads(doctor.cron_store().read_text()) if doctor.cron_store().exists() else []
    except Exception:
        return ""
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list) or any(
            isinstance(job, dict) and pathlib.Path(str(job.get("script") or "")).name == shim.name
            for job in jobs):
        return ""
    if shim.is_symlink():
        # Never followed or removed on its own authority — but named, like every other leftover.
        return (f"cron shim NOT removed: {shim} is a symlink (never followed) — no job runs it; "
                f"remove the link itself: rm -- {shlex.quote(str(shim))}")
    try:
        shim.unlink()
    except OSError as exc:
        return (f"cron shim NOT removed: {shim} ({exc.strerror or exc}) — no job runs it; remove "
                f"it by hand: rm -- {shlex.quote(str(shim))}")
    return f"cron shim removed: {shim} (no job runs it any more)"


def _purge_target(loop: dict) -> tuple[pathlib.Path | None, str]:
    """The state directory ``uninstall --purge`` may delete, or ``(None, why not)``.

    Only the default ``<hermes home>/state/diaktoros/<id>`` (``review-loops/<id>`` before the
    rename) is ever removed: a custom
    ``state_dir`` could be anything the operator typed, and a recursive delete is not the place
    to find out. No symlink anywhere below the Hermes home is followed.
    """
    base = config.home()
    default = config.default_state_dir(loop["id"])
    raw = pathlib.Path(str(loop.get("state_dir") or "")).expanduser()
    lid = shlex.quote(loop["id"])
    if os.path.normpath(str(raw)) != os.path.normpath(str(default)):
        return None, (f"state_dir {raw} is not the default {default}, so --purge will not delete "
                      "it: a custom directory could hold anything the operator pointed it at. "
                      "Check it holds only this loop's state, then run:\n"
                      f"  hermes dk uninstall --loop {lid} && "
                      f"rm -rf -- {shlex.quote(str(raw))}")
    for path in (base / "state", default.parent, default):
        if path.is_symlink():
            return None, (f"{path} is a symlink; --purge never follows one — it may point at "
                          "this loop's own (moved) state or somewhere else entirely, and this "
                          "command cannot tell which. Check where it points, uninstall without "
                          "--purge, remove the link, and remove the directory it points at by "
                          "hand if it is this loop's:\n"
                          f"  ls -ld -- {shlex.quote(str(path))}   # where it points\n"
                          f"  hermes dk uninstall --loop {lid} && "
                          f"rm -- {shlex.quote(str(path))}   # the link itself")
    if default.exists() and not default.is_dir():
        return None, f"{default} is not a directory"
    return default, ""


def _write_moved_repo(loop: dict, new: str) -> pathlib.Path:
    """Rename a loop's repository for ``migrate`` (#425), the one writer allowed to.

    ``_write_config`` refuses any repository change so a set/apply snapshot can never carry a loop
    (and its push policy) to another repository. ``migrate`` has verified that ``new`` is the
    *same* repository by id, so here the loop is re-read under the policy lock, must still name
    the repository it was read with, and only ``repo`` changes; the push policy is the current one.
    """
    with config.push_policy_lock():
        current = config.load_id(loop["id"])
        if current["repo"] != loop["repo"]:
            raise config.ConfigError("repository changed during migrate")
        return cli_common._write_config_locked({**current, "repo": new}, policy_change=True)


def _move_watchdog_job(*, dry_run: bool) -> list[str]:
    """``migrate`` (#425 stage 4): the shared watchdog job and its shim take the new name.

    The new shim is written and a job created with the old one's schedule and deliver target,
    read back, and only then the old job removed and its shim deleted. A failed create takes the
    new shim back out, so the old pair stays the live one.
    """
    new_name, old_name = config.HOST_FILES["watchdog_shim"]
    old_shim, new_shim = config.home() / old_name, config.home() / new_name
    if not old_shim.exists():
        return []
    jobs, error = cli_common._cron_jobs({"id": ""})
    if jobs is None:
        return [f"watchdog: NOT FINISHED — {error}"]
    old_job = config.WATCHDOG_JOBS[old_shim.name]
    old_jobs = [job for job in jobs if str(job.get("name") or "").strip() == old_job]
    if dry_run:
        return [f"watchdog: would move job {old_job!r} → {SHARED_JOB_NAME!r} and its shim "
                f"{old_shim.name} → {new_shim.name}"]
    watchdog = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "watchdog.py"
    new_shim.write_text(SHIM.format(watchdog=watchdog))
    new_shim.chmod(0o755)
    lines = [f"watchdog: shim written as {new_shim.name}"]
    if old_jobs and not any(str(job.get("name") or "").strip() == SHARED_JOB_NAME for job in jobs):
        schedule, deliver = doctor._job_schedule(old_jobs[0]), doctor._job_deliver(old_jobs[0])
        cmd = [cli_common._hermes_bin() or "hermes", "cron", "create", schedule, "--name", SHARED_JOB_NAME,
               "--no-agent", "--script", new_shim.name, "--deliver", deliver]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            failed = proc.returncode != 0 and (proc.stderr or proc.stdout).strip()[:200]
        except Exception as exc:
            failed = str(exc)
        after, _ = cli_common._cron_jobs({"id": ""})
        if failed or not any(str(job.get("name") or "").strip() == SHARED_JOB_NAME
                             for job in after or []):
            new_shim.unlink(missing_ok=True)
            return [f"watchdog: NOT FINISHED — the new job was not created"
                    + (f" ({failed})" if failed else "") + f"; the old job keeps running. "
                    f"Run it yourself, then migrate again: {shlex.join(cmd)}"]
        lines.append(f"watchdog: job {SHARED_JOB_NAME!r} created ({schedule}, deliver {deliver})")
    for job in old_jobs:
        job_id = str(job.get("id") or "")
        try:
            proc = subprocess.run([cli_common._hermes_bin() or "hermes", "cron", "remove", job_id],
                                  capture_output=True, text=True, timeout=120)
            ok = proc.returncode == 0
        except Exception:
            ok = False
        lines.append(f"watchdog: old job {job_id} ({old_job!r}) removed" if ok else
                     f"watchdog: NOT FINISHED — remove the old job yourself: "
                     f"hermes cron remove {shlex.quote(job_id)}")
    remaining, _ = cli_common._cron_jobs({"id": ""})
    if remaining is not None and not any(pathlib.Path(str(job.get("script") or "")).name
                                         == old_shim.name for job in remaining):
        old_shim.unlink(missing_ok=True)
        lines.append(f"watchdog: old shim {old_shim.name} removed")
    return lines


def _rename_loop(spec: str, *, dry_run: bool, admin: str | None) -> tuple[list[str], bool]:
    """``migrate --rename-loop OLD=NEW`` (#425 stage 3): ``(lines, refused)``.

    In an order a crash or a re-run cannot hurt: the routes are copied under the new names (the
    same secret, so a hook moved to one still verifies); each hook is moved to its new route's
    URL and pinged; then the state directory, today's pacing counts, the new loop file and the
    route intent record move; the old loop file goes; and an old route is removed only once every
    hook that pointed at it was answered on the new one. A refused ping puts its hook back.
    """
    from .. import (
        hook_ping,
        migrate,
        pacing,
        run_supervisor,
    )
    old_id, sep, new_id = spec.partition("=")
    tag = f"rename-loop: {old_id or '?'} → {new_id or '?'}"
    if not sep or old_id == new_id or not all(migrate.LOOP_ID.match(x or "") for x in (old_id, new_id)):
        return [f"{tag}: REFUSED — give OLD=NEW, two different loop ids of letters, digits, "
                "'.', '_' or '-' (at most 64)"], True
    directory = config.config_dir()
    try:
        old_loop = config.load_id(old_id) if (directory / f"{old_id}.json").exists() else None
        new_loop = config.load_id(new_id) if (directory / f"{new_id}.json").exists() else None
    except config.ConfigError as exc:
        return [f"{tag}: REFUSED — {exc}"], True
    if old_loop is None and new_loop is None:
        return [f"{tag}: REFUSED — no loop named {old_id!r}"], True
    if old_loop is not None and new_loop is not None and old_loop["repo"] != new_loop["repo"]:
        return [f"{tag}: REFUSED — a loop named {new_id!r} already exists for {new_loop['repo']}"], True
    base = old_loop or new_loop
    renames = migrate.route_renames(base, old_id, new_id)
    target = new_loop or migrate.renamed_loop(old_loop, new_id, renames)
    busy = migrate.busy_runs(run_supervisor.production_ledger(), base["repo"])
    if busy:
        return [f"{tag}: REFUSED — {busy} run(s) for {base['repo']} are in flight or uncertain; "
                "wait for them (or reconcile them), then run migrate again"], True
    registry = routes.all_routes()
    for old, new in renames.items():
        here = [registry[name] for name in (old, new) if isinstance(registry.get(name), dict)]
        if not here:
            return [f"{tag}: REFUSED — route {old} is in the registry under neither name; restore it "
                    f"first: `hermes dk doctor --loop {base['id']} --repair` (or `apply "
                    "--recreate-routes`)"], True
        if not all(route_intent.owned(entry) for entry in here):
            return [f"{tag}: REFUSED — route {old} or {new} is not one of this plugin's gates"], True
        if len(here) == 2 and here[0].get("secret") != here[1].get("secret"):
            return [f"{tag}: REFUSED — route {new} already exists with another secret"], True
    try:
        hooks = cli_common._hook_listing(base, admin)
    except config.ConfigError as exc:
        return [f"{tag}: REFUSED — cannot list the repo hooks: {exc}",
                f"  fix: {cli_common._hook_fix(base, exc, admin)}"], True
    role_of = {name: role for role, name in route_intent.routes_of(target).items()}
    moves = []
    for hook in hooks:
        name = routes.route_name_of(hook["config"]["url"])
        if name in renames or name in renames.values():
            new_name = renames.get(name, name)
            url = routes.url_for_profile(new_name, config.seat_profile(target, role_of.get(new_name, "")),
                                         target.get("host"))
            if not url:
                return [f"{tag}: REFUSED — cannot build the URL for route {new_name}"], True
            moves.append((hook, new_name, url))
    lines = [f"{tag}: routes " + (", ".join(f"{a} → {b}" for a, b in renames.items())
                                   or "(none follow the loop id)")
             + f"; {len(moves)} hook(s) to move and ping"]
    if dry_run:
        if old_loop is not None:
            lines.append(f"{tag}: " + migrate.move_state_dir(old_loop, target, dry_run=True))
        return lines, False
    copies = {new: dict(registry[old]) for old, new in renames.items()
              if new not in registry and isinstance(registry.get(old), dict)}
    if copies:
        routes.restore_entries(copies)
        lines.append(f"{tag}: routes copied under the new names (same secrets): {', '.join(copies)}")
    proven, unproven = set(), set()
    for hook, new_name, url in moves:
        before = hook["config"]["url"]
        if not routes.serves_route_url(before, url):
            cli_common._patch_hook_url(base, hook["id"], url, admin, require_secret=True,
                            insecure_ssl=hook["config"].get("insecure_ssl"))
        status, said = hook_ping.ping(base, hook["id"], admin)
        if status == hook_ping.OK:
            proven.add(new_name)
            lines.append(f"{tag}: hook {hook['id']} → {new_name}: {said}")
        elif status == hook_ping.SILENT:
            unproven.add(new_name)
            lines.append(f"{tag}: hook {hook['id']} → {new_name}: {said} — the old route stays "
                         "until a ping is answered; run migrate again later")
        else:
            back = ""
            if before != url and routes.route(routes.route_name_of(before)):
                cli_common._patch_hook_url(base, hook["id"], before, admin, require_secret=True,
                                insecure_ssl=hook["config"].get("insecure_ssl"))
                back = f"; hook {hook['id']} put back on {before}"
            return lines + [f"{tag}: REFUSED — {said}{back}; nothing else was moved"], True
    if old_loop is not None:
        lines.append(f"{tag}: " + migrate.move_state_dir(old_loop, target, dry_run=False))
    if pacing.rename_loop(old_id, new_id):
        lines.append(f"{tag}: today's turn and token counts moved to {new_id}")
    if new_loop is None:
        cli_common._write_config(target)
        lines.append(f"{tag}: loop file written as {new_id}.json")
    route_intent.record_live(target, list(renames.values()))
    route_intent.forget(target, list(renames))
    (directory / f"{old_id}.json").unlink(missing_ok=True)
    live = routes.all_routes()
    gone = [old for old, new in renames.items() if old in live and new not in unproven]
    if gone:
        routes.restore_entries({old: None for old in gone})
        lines.append(f"{tag}: old routes removed: {', '.join(gone)}")
    if unproven:
        lines.append(f"{tag}: NOT FINISHED — not proven yet: {', '.join(sorted(unproven))}")
    return lines, bool(unproven)


def cmd_migrate(args) -> int:
    """Move an install from the ``hermes-review-loop`` plugin to this one (#425); see ``migrate``.

    Run between turns. The install is paused for the whole run (#431): gates defer, the worker
    claims nothing, the watchdog sweeps nothing. Exit 1 when a step was refused or is not
    finished (a run in flight, a shim this plugin did not write, a hook not yet proven);
    re-running finishes what an interrupted run began.
    """
    from .. import migrate
    if args.dry_run:
        return _migrate(args)
    try:
        with migrate.hold() as left:
            if left is not None:
                print(f"migrate: picking up — {migrate.describe(left)}")
            return _migrate(args)
    except migrate.MigrationBusy as exc:
        print(f"cannot migrate: {exc}")
        return 2


def _migrate(args) -> int:
    from .. import backup, migrate, run_supervisor
    try:
        loops = config.all_loops()
    except config.ConfigError as exc:
        print(f"cannot migrate: {exc}")
        return 2
    if args.dry_run:
        print(f"backup: would write {backup.default_out()} before anything moves")
    else:
        # Before the first move (#496): migrate has no undo, the archive is the undo.
        try:
            archive = backup.create()
        except (backup.BackupError, OSError, sqlite3.Error, config.ConfigError) as exc:
            print(f"cannot migrate: the backup before it failed ({exc}); nothing was moved")
            return 2
        print(f"backup: {archive} (restore with `hermes dk restore {shlex.quote(str(archive))}`)")
        print(f"backup: {backup.WARNING}")
    lines = migrate.settings_step(cli_common._CTX, dry_run=args.dry_run)
    watchdog = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "watchdog.py"
    if not args.dry_run:
        # Before anything moves, every route and the watchdog run this plugin's code, which
        # honours the pause. Until now they may run the old plugin's, which does not: a delivery
        # mid-migration then wrote under the old file names (seen on the first live upgrade).
        lines += migrate.shim_step(loops, cli_common._write_watchdog_shim, config.watchdog_shim(),
                                   SHIM.format(watchdog=watchdog), dry_run=False)
    lines += _move_watchdog_job(dry_run=args.dry_run)
    # Then the host files: everything after reads and writes them under their new names.
    lines += migrate.files_step(cli_common._write_config, dry_run=args.dry_run)
    if not args.dry_run:
        loops = config.all_loops()                 # the loop files may live elsewhere now
    lines += migrate.repo_step(loops, _write_moved_repo, dry_run=args.dry_run,
                               ledger=run_supervisor.production_ledger())
    if getattr(args, "rename_loop", None):
        # After the repository step: a hook is written through the repository's current name.
        renamed, _ = _rename_loop(args.rename_loop, dry_run=args.dry_run,
                                  admin=getattr(args, "admin_token", None))
        lines += renamed
    if not args.dry_run:
        loops = config.all_loops()                 # a moved repository or id is the loop's name now
    lines += migrate.shim_step(loops, cli_common._write_watchdog_shim, config.watchdog_shim(),
                               SHIM.format(watchdog=watchdog), dry_run=args.dry_run)
    if not args.dry_run:
        lines += migrate.doctor_step(loops)
    lines += migrate.old_plugin_note(config.home() / "plugins")
    print("\n".join(lines))
    if args.dry_run:
        print("dry run: nothing was written")
    return 1 if any(word in line for line in lines
                    for word in ("REFUSED", "NOT rewritten", "NOT FINISHED")) else 0


def cmd_backup(args) -> int:
    """``backup`` (#496): one 0600 archive of everything the plugin owns; see ``backup``."""
    from .. import backup
    try:
        archive = backup.create(pathlib.Path(args.out) if args.out else None)
    except (backup.BackupError, OSError, sqlite3.Error, config.ConfigError) as exc:
        print(f"cannot back up: {exc}")
        return 2
    print(f"backup written: {archive}")
    print(f"warning: {backup.WARNING}")
    return 0


def _restore_cron(job: dict | None) -> list[str]:
    """Recreate the watchdog job through ``hermes cron`` and read it back, as migrate does."""
    if not job:
        return ["cron: the backup holds no watchdog job"]
    jobs, error = cli_common._cron_jobs({"id": ""})
    if jobs is None:
        return [f"cron: NOT FINISHED — {error}"]
    if any(str(j.get("name") or "").strip() in SHARED_JOB_NAMES for j in jobs):
        return ["cron: the watchdog job exists — kept"]
    cmd = [cli_common._hermes_bin() or "hermes", "cron", "create", job["schedule"], "--name", job["name"],
           "--no-agent", "--script", config.watchdog_shim().name, "--deliver", job["deliver"]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        failed = proc.returncode != 0 and (proc.stderr or proc.stdout).strip()[:200]
    except Exception as exc:
        failed = str(exc)
    after, _ = cli_common._cron_jobs({"id": ""})
    if failed or not any(str(j.get("name") or "").strip() == job["name"] for j in after or []):
        return ["cron: NOT FINISHED — the job was not created"
                + (f" ({failed})" if failed else "") + f"; run it yourself: {shlex.join(cmd)}"]
    return [f"cron: job {job['name']!r} created ({job['schedule']}, deliver {job['deliver']})"]


def _restore(args, manifest: dict, archive: pathlib.Path) -> int:
    from .. import backup, migrate
    lines = [f"restore: {len(manifest['files'])} file(s), {len(manifest.get('routes') or {})} "
             f"route(s), loops: {', '.join(manifest.get('loops') or []) or 'none'}"]
    lines += [f"restore: NOT FINISHED — token file missing: {line}"
              for line in backup.missing_tokens(manifest)]
    lines += [f"restore: will overwrite {name}" for name in backup.existing(manifest)]
    lines += [f"restore: writes a loop state directory outside the Hermes home: {path}"
              for path in backup.outside_state_dirs(manifest)]
    problems = backup.unsafe(manifest,
                             allowed_state_dirs=getattr(args, "allow_state_dir", None) or (),
                             archive=archive)
    if problems:
        print("\n".join(lines + [f"restore: REFUSED — {p}" for p in problems[:20]]
                         + ["nothing was written"]))
        return 2
    if args.dry_run:
        print("\n".join(lines + ["dry run: nothing was written"]))
        return 0
    backup.put_files(archive, manifest)
    lines.append(f"restore: {len(manifest['files'])} file(s) put back")
    if manifest.get("routes"):
        routes.restore_entries(manifest["routes"])
        lines.append(f"restore: {len(manifest['routes'])} route entr(ies) put back")
    loops = config.all_loops()
    watchdog = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "watchdog.py"
    if not config.watchdog_shim().is_file():
        cli_common._write_watchdog_shim()
    lines += migrate.shim_step(loops, cli_common._write_watchdog_shim, config.watchdog_shim(),
                               SHIM.format(watchdog=watchdog), dry_run=False)
    lines += _restore_cron(manifest.get("cron"))
    lines += migrate.doctor_step(loops)
    print("\n".join(lines))
    return 1 if any(word in line for line in lines
                    for word in ("REFUSED", "NOT rewritten", "NOT FINISHED")) \
        or any(line.startswith("doctor:") and line != "doctor: every check verified"
               for line in lines) else 0


def cmd_restore(args) -> int:
    """``restore`` (#496): put a backup back, paused with migrate's marker; see ``backup``."""
    from .. import backup, migrate
    archive = pathlib.Path(args.file).expanduser()
    try:
        manifest = backup.read_manifest(archive)
        if args.dry_run:
            return _restore(args, manifest, archive)
        with migrate.hold():
            busy = migrate.runs_in_flight()
            if busy:
                print(f"cannot restore: {busy} run(s) are in flight or uncertain; wait for them "
                      "(or reconcile them), then run restore again")
                return 2
            clash = backup.existing(manifest)
            if clash and not args.force:
                print("cannot restore: existing state would be overwritten (use --force):\n  "
                      + "\n  ".join(clash[:20])
                      + (f"\n  … {len(clash) - 20} more" if len(clash) > 20 else ""))
                return 2
            return _restore(args, manifest, archive)
    except (migrate.MigrationBusy, migrate.RunsUnknown) as exc:
        print(f"cannot restore: {exc}")
        return 2
    except (backup.BackupError, OSError, sqlite3.Error, config.ConfigError) as exc:
        print(f"cannot restore: {exc}")
        return 2


def _uninstall_preflight(loop: dict) -> list[str]:
    """Reasons a later uninstall step would fail on a shared file, read before anything is removed.

    The route registry is rewritten by step 3, after the hooks and the watchdog job are already
    gone; it must parse now. (The cron store was read just before this; the intent record's own
    reader tolerates a bad file — it heals nothing then.)
    """
    problems = []
    path = routes.subs_path()
    if path.exists() or path.is_symlink():
        try:
            data = json.loads(path.read_text())
        except Exception as exc:
            problems.append(f"route registry {path} cannot be read ({type(exc).__name__}: "
                            f"{exc}) — fix or restore it; nothing was removed")
        else:
            if not isinstance(data, dict):
                problems.append(f"route registry {path} is not a JSON object — fix or restore "
                                "it; nothing was removed")
    return problems


def _unloadable_teardown_advice(loop_id: str) -> None:
    """For a loop file the loader refuses: what may still be live, and how to find it.

    ``uninstall`` will not act on a config it cannot validate — deleting hooks or jobs on a
    guess is the wrong way to fail — but it can still read the raw file for the repo, the route
    names and the job name, and hand over the commands to look them up.
    """
    try:
        raw = json.loads((config.config_dir() / f"{loop_id}.json").read_text())
    except Exception:
        return
    if not isinstance(raw, dict) or str(raw.get("repo") or "").count("/") != 1:
        return
    raw = {**raw, "id": loop_id, "repo": str(raw["repo"]).strip().lower()}
    print("it may still have live repo hooks and a watchdog job; nothing was touched. "
          "Look them up with:")
    if cli_common._hook_route_names(raw):
        print(f"  {_hook_find_command(raw)}   # hook ids on its route names — check each URL "
              "before deleting: `gh api -X DELETE repos/<owner>/<repo>/hooks/<id>`")
    jobs, _ = cli_common._cron_jobs(raw)
    for job in jobs or []:
        print(f"  hermes cron remove {shlex.quote(str(job.get('id') or '<id>'))}   "
              f"# {watchdog_job_name(raw)}")
    print("then fix the file (the reason above) and re-run `hermes dk uninstall "
          f"--loop {shlex.quote(loop_id)}`, or remove what is listed by hand and delete "
          f"{config.config_dir() / (loop_id + '.json')}")


def _uninstall_incomplete(removed: list[str], left: list[str], keep_hooks: bool,
                          commands: list[str]) -> int:
    """A late step failed after earlier ones were done: say which, and how to finish. Exit 2.

    Past this point the config may be gone, so a re-run cannot finish the job — the printed
    commands do.
    """
    print("uninstall INCOMPLETE — removed: " + (", ".join(removed) or "nothing else")
          + "; left behind: " + ("the repo hooks (--keep-hooks), " if keep_hooks else "")
          + ", ".join(left))
    print("fix what refused (the permissions of the path above), then finish with:")
    for command in commands:
        print(f"  {command}")
    return 2


def _uninstall_refused(loop: dict, reasons: list[str], left_hooks: list[int],
                       args, unattributed: bool = False, accepted_unread: bool = False) -> int:
    """Refuse an uninstall with the exact commands that finish it. The config is still there.

    ``accepted_unread`` is the structured fact that every DELETE GitHub accepted but the listing
    read-back could not confirm: the caller knows it, so the remedy is chosen by that flag rather
    than by pattern-matching a reason's wording (#131). Rewording the message that produced a
    reason must not silently change which remedy the operator is given.
    """
    lid = shlex.quote(loop["id"])
    print("refused: uninstall stopped before removing routes or config — nothing below the "
          "failure was touched:")
    for reason in reasons:
        print(f"  {reason}")
    if unattributed:
        print("they may be this install's or another install's (same repo, same route names): "
              "look at each one's URL before deleting anything:")
        print(f"  {_hook_find_command(loop)}   # the ids")
        print(f"  gh api {shlex.quote(f'repos/' + loop['repo'] + '/hooks/<id>')} --jq .config.url"
              "   # where each posts")
        print("then either set this loop's host (`hermes dk set --loop "
              f"{lid} --host https://your-gateway.example`) so uninstall can tell its own hooks "
              "apart, or delete the ones that are this install's by hand")
    elif accepted_unread and not left_hooks:
        # Chosen by the caller's structured fact, never by the reason's wording: a reworded
        # message must not flip this honest case into a different remedy (#131).
        print("every DELETE was accepted, but the hook listing could not be read back to confirm "
              "it — look before re-running (it lists any id that is somehow still there):")
        print(f"  {_hook_find_command(loop)}")
    elif left_hooks:
        # Only ids a listing actually showed at this loop's route URLs are called live, and only
        # they get DELETE commands.
        print("the loop's repo hooks are still live. Delete them with a token that has "
              "`admin:repo_hook` (or classic `repo`):")
        for command in cli_common._hook_delete_commands(loop, left_hooks):
            print(f"  {command}")
        print("or map such a token on this loop and let uninstall do it:")
        print(f"  hermes dk uninstall --loop {lid} --admin-token <login>")
    elif any(reason.startswith(("hook", "could not")) for reason in reasons):
        # Nothing was read (or read back), so nothing is known about which hooks exist — or
        # whose they are. No liveness claim, and no DELETE on a guess.
        print("the hook listing could not be read or confirmed, so this command does not know "
              "which hooks exist; nothing was deleted on a guess. Look first — check each one's "
              "URL before deleting anything:")
        print(f"  {_hook_find_command(loop)}   # the ids")
        print(f"  gh api {shlex.quote(f'repos/' + loop['repo'] + '/hooks/<id>')} --jq .config.url"
              "   # where each posts")
    if any(reason.startswith("cron") for reason in reasons):
        jobs, _ = cli_common._cron_jobs(loop)
        for job in jobs or []:
            print(f"  hermes cron remove {shlex.quote(str(job.get('id') or '<id>'))}")
    print("then re-run:")
    admin = getattr(args, "admin_token", "") or ""
    print(f"  hermes dk uninstall --loop {lid}"
          + (f" --admin-token {shlex.quote(admin)}" if admin else "")
          + (" --keep-config" if args.keep_config else "")
          + (" --purge" if getattr(args, "purge", False) else ""))
    print("(`--keep-hooks` uninstalls anyway and leaves the hooks live — they keep posting to "
          "routes that no longer exist)")
    return 2


def cmd_uninstall(args) -> int:
    """The inverse of ``init``: hooks, cron job, routes, config and (``--purge``) state.

    Order matters. Everything that needs the loop config — its hook routes, its token mapping, its
    job name — is undone *first*, while the config still exists; if any of it cannot be done the
    command refuses with the commands that finish it, and the config stays so they still run.
    """
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"cannot uninstall: {exc}")
        _unloadable_teardown_advice(args.loop)
        return 2
    keep_hooks = getattr(args, "keep_hooks", False)
    purge = getattr(args, "purge", False)
    admin = getattr(args, "admin_token", "") or ""
    target = None
    if purge:
        if args.keep_config:
            print("refused: --purge removes the state the kept config points at; drop one of "
                  "--purge / --keep-config")
            return 2
        target, why = _purge_target(loop)
        if target is None:
            print(f"refused: {why}")
            return 2
        try:
            busy = cli_common._busy_seats(loop, {"reviewer", "fixer"})
        except OSError as exc:
            # Reading the in-flight marks takes the state lock inside the directory about to be
            # deleted; if that cannot be opened, nothing can be proved idle — refuse, untouched.
            where = f" ({exc.filename})" if getattr(exc, "filename", None) else ""
            print(f"refused: --purge cannot check for a run in flight — the state directory "
                  f"{target} cannot be read: {exc.strerror or exc}{where}. Nothing was removed. "
                  "Fix its permissions and re-run, or uninstall without --purge and remove it "
                  f"by hand afterwards: rm -rf -- {shlex.quote(str(target))}")
            return 2
        if busy:
            print("refused: --purge would delete the state of a run in flight: " + "; ".join(busy))
            return 2
    refusal = cli_common.admin_unmapped(loop, admin)
    if refusal:
        print(f"refused: {refusal}")
        return 2
    jobs, error = cli_common._cron_jobs(loop)
    if jobs is None:
        return _uninstall_refused(loop, [f"cron: {error}"], [], args)
    # Every shared file a later step rewrites is read *now*, before a hook or a job is touched:
    # a registry that will not parse must refuse here, not raise after the destructive steps.
    problems = _uninstall_preflight(loop)
    if problems:
        return _uninstall_refused(loop, problems, [], args)
    # What is already gone, for the summary if a later step (the state purge) fails.
    removed: list[str] = []
    # 1. Stop deliveries: delete the repo hooks while the config still names their routes.
    if keep_hooks:
        print("hooks: kept (--keep-hooks) — they stay live and post to routes about to be removed;"
              " find them with:")
        print(f"  {_hook_find_command(loop)}")
    elif not str(loop.get("host") or "").strip():
        # A host-less loop has no gateway origin to tell its own hooks from another install's —
        # and it may once have had one (a host blanked by hand). So look before skipping: any hook
        # posting to its route names may be this install's, live, and is refused with the
        # commands that delete it; only an empty answer lets the uninstall go on.
        listing, error = gh.hooks_read(loop, admin or loop.get("read_token"))
        if error:
            return _uninstall_refused(loop, [f"could not read the repo's hooks: {error}"], [], args)
        matching = [hook for hook in listing
                    if not isinstance(hook, dict) or doctor.hook_route_name(hook)
                    in cli_common._hook_route_names(loop)]
        if any(not isinstance(hook, dict) or not isinstance(hook.get("id"), int)
               for hook in matching):
            # _classify_hooks' rule: an entry that cannot be identified makes the listing
            # untrustworthy — never "no hooks".
            return _uninstall_refused(loop, ["could not read the repo's hooks: invalid hook "
                                             "listing (an entry on the loop's route names has no "
                                             "integer id)"], [], args)
        named = sorted(hook["id"] for hook in matching)
        if named:
            # Without a host there is no route URL to compare with, so none of these can be
            # attributed to this install: never hand out DELETE commands for them.
            return _uninstall_refused(loop, [
                f"hooks {', '.join(map(str, named))} post to this loop's route names, and the loop "
                "has no host to tell whether they are this install's (a blanked host leaves its "
                "hooks behind) or another install's on the same repo — nothing was deleted"],
                [], args, unattributed=True)
        print("hooks: none — the loop has no host, and no repo hook posts to its route names")
    else:
        done, failures, left, accepted_unread = _delete_loop_hooks(loop, admin or None)
        for line in done:
            print(line)
        if failures or left:
            if left and not failures:
                failures = [f"hook {hook_id} still present after DELETE" for hook_id in left]
            return _uninstall_refused(loop, failures, left, args,
                                      accepted_unread=accepted_unread)
        if not done:
            print("hooks: none of this loop's routes has a repo hook")
        elif any(line.endswith(" deleted") for line in done):
            removed.append("repo hooks")
    # 2. Stop the watchdog.
    done, failures = _remove_cron(loop)
    for line in done:
        print(line)
    if failures:
        return _uninstall_refused(loop, failures, [], args)
    if any(line.startswith("cron job removed") for line in done):
        removed.append("watchdog job")
    shim_line = _remove_unused_shim()
    leftovers: list[tuple[str, str]] = []          # (what, the command that removes it)
    if shim_line:
        print(shim_line)
        if shim_line.startswith("cron shim removed"):
            removed.append("cron shim")
        else:
            shim = config.watchdog_shim()
            leftovers.append((f"the cron shim {shim}", f"rm -- {shlex.quote(str(shim))}"))
    # 3. Forget first: a route the operator removed must not be put back by the next watchdog
    # sweep's self-heal (which only ever restores routes still in the intent record).
    lid = shlex.quote(loop["id"])
    finish = f"hermes dk uninstall --loop {lid}"
    try:
        mine = dict(cli_common._routes_of(loop))
        app = mine.get("app")
        if app and any(other.get("id") != loop["id"] and cli_common._routes_of(other).get("app") == app
                       for other in config.all_loops()):
            # #1239: the App's route is shared; another loop it delivers for still needs it.
            print(f"route kept: {app} (another loop still receives this App's events through it)")
            mine.pop("app")
        route_intent.forget(loop, mine.values())
        for name in mine.values():
            if name and routes.remove_route(name):
                print(f"route removed: {name}")
                if "routes" not in removed:
                    removed.append("routes")
    except (OSError, ValueError) as exc:
        # Hooks and the job are already gone; the config is still here, so a re-run finishes
        # once the registry (or intent record) can be written — say so, never a traceback.
        print(f"routes NOT removed: {exc}")
        left = [what for what, _ in leftovers] + [
            f"the routes {', '.join(n for n in cli_common._routes_of(loop).values() if n)} in "
            f"{routes.subs_path()}", f"the loop config (so `{finish}` can finish)"]
        commands = [cmd for _, cmd in leftovers] + [f"{finish}   # once the file above is "
                                                    "readable and writable again"]
        return _uninstall_incomplete(removed, left, keep_hooks, commands)
    try:
        others = [other for other in config.all_loops()
                  if not (other["id"] == loop["id"] and other.get("repo") == loop.get("repo"))]
        for line in gate_shims.remove(loop, others):
            print(line)
    except (OSError, config.ConfigError) as exc:
        print(f"gate shims left in place (another loop may still need them): {exc}")
    if not args.keep_config:
        path = config.config_dir() / f"{loop['id']}.json"
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                # Hooks, cron and routes are already gone: report the half-done state and the
                # one command that finishes it, never a traceback.
                print(f"config NOT removed: {path} — {exc.strerror or exc}")
                left = [what for what, _ in leftovers] + [f"the loop config {path}"]
                commands = [cmd for _, cmd in leftovers] + [f"rm -f -- {shlex.quote(str(path))}"]
                if target is not None:
                    left.append(f"the state directory {target} (not attempted)")
                    commands.append(f"rm -rf -- {shlex.quote(str(target))}")
                return _uninstall_incomplete(removed, left, keep_hooks, commands)
            print(f"config removed: {path}")
            removed.append("config")
    if target is not None:
        if target.is_symlink():
            # Swapped for a link after the preflight vetted it: never followed. Everything above
            # is already done, so this is a partial decommission, reported as one.
            print(f"state NOT removed: {target} became a symlink after it was checked — never "
                  "followed")
            return _uninstall_incomplete(
                removed, [what for what, _ in leftovers]
                + [f"the state directory {target} (now a symlink: check where it points, "
                   "remove the link, and that directory by hand if it is this loop's)"],
                keep_hooks, [cmd for _, cmd in leftovers]
                + [f"ls -ld -- {shlex.quote(str(target))}   # where it points",
                   f"rm -- {shlex.quote(str(target))}   # the link itself"])
        if target.exists():
            refused: list[tuple[str, str]] = []

            def note(_func, path, exc_info) -> None:
                # rmtree's own exception names only the entry, relative to an open directory:
                # keep the full path of every refusal, and delete everything else it can.
                exc = exc_info[1] if isinstance(exc_info, tuple) else exc_info
                refused.append((str(path), getattr(exc, "strerror", None) or str(exc)))

            try:
                if sys.version_info >= (3, 12):
                    shutil.rmtree(target, onexc=note)
                else:
                    shutil.rmtree(target, onerror=note)
            except OSError as exc:
                refused.append((str(getattr(exc, "filename", None) or target),
                                exc.strerror or str(exc)))
            if refused or target.exists():
                # Everything above is already undone and the config is gone, so a re-run cannot
                # finish this: say what happened and hand over the one command that does.
                path, why = refused[0] if refused else (str(target), "still present")
                more = f" (and {len(refused) - 1} more)" if len(refused) > 1 else ""
                print(f"state NOT removed: {target} — {why}: {path}{more}")
                return _uninstall_incomplete(
                    removed, [what for what, _ in leftovers]
                    + [f"the state directory {target} (whatever the delete could remove "
                       "is gone; the rest is still there)"],
                    keep_hooks, [cmd for _, cmd in leftovers]
                    + [f"rm -rf -- {shlex.quote(str(target))}"])
            print(f"state removed: {target}")
    elif not args.keep_config:
        _, why = _purge_target(loop)
        raw = pathlib.Path(str(loop.get("state_dir") or "")).expanduser()
        if why:
            print(f"state kept: {raw} — not the default location or not a plain directory, so "
                  f"--purge would not remove it; check it, then: rm -rf -- {shlex.quote(str(raw))}")
        else:
            print(f"state kept: {raw} (pass --purge to remove it)")
    if not args.keep_config:
        # The last loop gone (#113): forget that a run ledger existed, so a later fresh install
        # is not reported as a vanished ledger (run_supervisor.presence_marker). After the state
        # step, so a --purge has removed the state dir first.
        from ..run_supervisor import forget_ledger_presence, presence_marker
        if not any(config.config_dir().glob("*.json")) and forget_ledger_presence():
            print(f"ledger presence marker removed: {presence_marker()}")
    if leftovers:
        # Everything else went; the shim did not. That is not a clean uninstall.
        return _uninstall_incomplete(removed, [what for what, _ in leftovers], keep_hooks,
                                     [cmd for _, cmd in leftovers])
    return 0


# -- the parser for this module's commands (#676 step 7) --------------------------------------


def add_migrate(sub) -> None:  # noqa: ANN001
    """The ``migrate`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    move = sub.add_parser("migrate", help="Move an install from the hermes-review-loop plugin to "
                                          "this one: settings, a renamed repository, shims (#425)")
    move.add_argument("--dry-run", action="store_true",
                      help="report every step and write nothing")
    move.add_argument("--rename-loop", metavar="OLD=NEW", default=None,
                      help="also give a loop a new id: its file, default state directory, "
                           "routes and the URLs its repo hooks post to (each hook is pinged "
                           "before the old route goes)")
    move.add_argument("--admin-token", default=None, metavar="LOGIN",
                      help="mapped login whose token may edit the repo hooks (--rename-loop)")
    move.set_defaults(func=cmd_migrate)


def add_backup(sub) -> None:  # noqa: ANN001
    """The ``backup`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    bak = sub.add_parser("backup", help="Write one 0600 archive of everything the plugin owns "
                                        "(loops, ledger, state, routes, watchdog job) (#496)")
    bak.add_argument("--out", metavar="FILE", default=None,
                     help="where to write it (default: $HERMES_HOME/backups/…); never "
                          "overwrites a file")
    bak.set_defaults(func=cmd_backup)


def add_restore(sub) -> None:  # noqa: ANN001
    """The ``restore`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    rest = sub.add_parser("restore", help="Put a backup back: files, routes, shims, the "
                                          "watchdog job; ends with doctor (#496)")
    rest.add_argument("file", metavar="FILE", help="an archive `backup` wrote")
    rest.add_argument("--dry-run", action="store_true",
                      help="report what would be restored and overwritten; write nothing")
    rest.add_argument("--force", action="store_true",
                      help="overwrite existing state (refused without it)")
    rest.add_argument("--allow-state-dir", action="append", default=None, metavar="DIR",
                      help="write this loop state directory, which the archive declares "
                           "outside the Hermes home (the dry run lists them; repeat per "
                           "directory)")
    rest.set_defaults(func=cmd_restore)


def add_uninstall(sub) -> None:  # noqa: ANN001
    """The ``uninstall`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    uninstall = sub.add_parser("uninstall", help="Remove a loop: its repo hooks, cron job, "
                               "routes and config (refuses rather than leave live hooks)")
    uninstall.add_argument("--loop", required=True,
                           help="loop id (its config file name; `list` shows them)")
    uninstall.add_argument("--keep-config", action="store_true",
                           help="remove hooks, cron job and routes but keep the loop config file")
    uninstall.add_argument("--admin-token", default="",
                           help="login whose token can delete hooks (admin:repo_hook or repo)")
    uninstall.add_argument("--keep-hooks", action="store_true",
                           help="leave the repo hooks live (explicit opt-out)")
    uninstall.add_argument("--purge", action="store_true",
                           help="also delete the loop's default state directory")
    uninstall.set_defaults(func=cmd_uninstall)
