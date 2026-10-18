"""``hermes dk backup`` / ``hermes dk restore`` (#496), and the backup ``migrate`` takes first.

One gzip tarball holds everything this plugin owns: the loop files, the runtime file, pacing and
the gate-failure ledger, the run ledger (through SQLite's online backup, so it is consistent
while the loop is live), each loop's state directory, this plugin's route-registry entries, and
the watchdog job's schedule and delivery target.

What the archive holds is a ``manifest.json`` (where every member goes back to, the routes, the
job) plus the members. Route entries carry their HMAC secrets so restored hooks keep working, so
the archive is created 0600 and nothing in it is ever printed. Left out: PATs, Hermes profiles
and model logins; ``restore`` checks the token files the loops name and lists any that are missing.

Nothing here has a setting: the only knob is ``--out``. An output directory or retention setting
would have to be settable from the form, ``init``, ``setup``, ``set`` and the loop file, and none
exists yet.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import shlex
import sqlite3
import tarfile
import tempfile
import time

from .ledger import LOCK_WAIT_S
from .config import credentials
from . import config, gate_shims, route_intent, routes, run_supervisor

VERSION = 1
MANIFEST = "manifest.json"
WARNING = ("this archive holds route secrets (webhook HMAC keys): keep it private; "
           "it was written mode 0600 and its contents are never printed")


class BackupError(RuntimeError):
    pass


def default_out() -> pathlib.Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = config.home() / "backups"
    path = base / f"diaktoros-backup-{stamp}.tar.gz"
    n = 1
    while path.exists() or path.is_symlink():   # two runs in one second must not collide
        n += 1
        path = base / f"diaktoros-backup-{stamp}-{n}.tar.gz"
    return path


def _regular_files(root: pathlib.Path) -> list[pathlib.Path]:
    if root.is_symlink() or not root.exists():
        return []
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())


def _snapshot_ledger(src: pathlib.Path, dest: pathlib.Path) -> None:
    """SQLite's online backup: a consistent copy while other connections write."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        target = sqlite3.connect(dest)  # private new file
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def watchdog_job() -> dict | None:
    """The shared watchdog job's schedule and delivery target, as the scheduler stores them."""
    from . import doctor
    from .cli import common as cli_common  # noqa: PLC0415 - heavy modules
    jobs, _ = cli_common._cron_jobs({"id": ""})
    for job in jobs or []:
        if str(job.get("name") or "").strip() in cli_common.SHARED_JOB_NAMES:
            return {"name": str(job["name"]).strip(), "schedule": doctor._job_schedule(job),
                    "deliver": doctor._job_deliver(job)}
    return None


def token_paths(loop: dict) -> dict[str, str]:
    return {str(login): str(path) for login, path in (loop.get("tokens") or {}).items()}


def create(out: pathlib.Path | None = None) -> pathlib.Path:
    """Write the archive and return its path. Refuses to overwrite a file."""
    out = pathlib.Path(out).expanduser() if out else default_out()
    out = out.absolute()
    loops = config.all_loops()
    files: list[tuple[pathlib.Path, pathlib.Path | None, str]] = []   # (target, source, kind)
    cfg = config.config_dir()
    for path in sorted(cfg.glob("*.json")) if cfg.is_dir() else []:
        if path.is_file() and not path.is_symlink():
            files.append((path, path, "file"))
    for key in ("runtime", "pacing"):
        path = config.host_path(key)
        if path.is_file() and not path.is_symlink():
            files.append((path, path, "file"))
    for key in ("gate_failures",):
        for path in _regular_files(config.host_path(key)):
            files.append((path, path, "file"))
    for loop in loops:
        for path in _regular_files(config.state_dir(loop)):
            files.append((path, path, "file"))
    ledger = run_supervisor.production_ledger()
    manifest = {"version": VERSION, "created": time.time(), "loops": [l["id"] for l in loops],
                "files": [], "state_dirs": sorted({str(config.state_dir(l)) for l in loops}),
                "routes": {name: entry for name, entry in routes.all_routes().items()
                           if isinstance(entry, dict) and route_intent.owned(entry)},
                "cron": watchdog_job(),
                "tokens": {l["id"]: token_paths(l) for l in loops}}
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise BackupError(f"{out} already exists — pick another --out") from None
    try:
        with os.fdopen(fd, "wb") as stream, tarfile.open(fileobj=stream, mode="w:gz") as tar, \
                tempfile.TemporaryDirectory() as scratch:
            if ledger.is_file():
                snap = pathlib.Path(scratch) / "ledger"
                _snapshot_ledger(ledger, snap)
                files.append((ledger, snap, "ledger"))
            for index, (target, source, kind) in enumerate(files):
                member = f"files/{index}"
                try:
                    tar.add(source, arcname=member, recursive=False)
                except OSError as exc:
                    raise BackupError(f"cannot read {source}: {exc}") from exc
                manifest["files"].append({"member": member, "path": str(target), "kind": kind})
            raw = json.dumps(manifest, indent=1).encode()
            info = tarfile.TarInfo(MANIFEST)
            info.size, info.mtime, info.mode = len(raw), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(raw))
        os.chmod(out, 0o600)
    except BaseException:
        out.unlink(missing_ok=True)
        raise
    return out


def read_manifest(archive: pathlib.Path) -> dict:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.getmember(MANIFEST)
            manifest = json.load(tar.extractfile(member))
    except (OSError, KeyError, ValueError, tarfile.TarError) as exc:
        raise BackupError(f"{archive} is not a diaktoros backup ({type(exc).__name__}: {exc})")
    if not isinstance(manifest, dict) or manifest.get("version") != VERSION \
            or not isinstance(manifest.get("files"), list):
        raise BackupError(f"{archive}: unsupported backup manifest")
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) \
                or not isinstance(entry.get("member"), str) \
                or not pathlib.PurePath(entry["path"]).is_absolute() \
                or ".." in pathlib.PurePath(entry["path"]).parts:
            raise BackupError(f"{archive}: manifest names an unsafe path")
    return manifest


def outside_state_dirs(manifest: dict) -> list[str]:
    """The archive's declared loop state directories that lie outside the Hermes home and the
    loop files' directory (a loop's ``state_dir`` is configurable)."""
    roots = [pathlib.Path(os.path.realpath(r)) for r in (config.home(), config.config_dir())]
    out = []
    for raw in manifest.get("state_dirs") or []:
        path = pathlib.Path(os.path.abspath(str(raw)))
        if not any(path == root or root in path.parents for root in roots):
            out.append(str(path))
    return sorted(set(out))


def archived_loops(archive: pathlib.Path, manifest: dict) -> list[dict]:
    """The loop configs that will be in place once this archive is restored: the archive's own
    loop files, plus every live loop it does not replace. An archived loop file that does not
    load is left out, so the routes it would have claimed are judged as unclaimed (stricter)."""
    loops: dict = {}
    with tarfile.open(archive, "r:gz") as tar:
        for entry in manifest["files"]:
            target = pathlib.Path(entry["path"])
            if target.suffix != ".json" or target.parent != config.config_dir():
                continue
            try:
                loop = config.normalize(json.load(tar.extractfile(tar.getmember(entry["member"]))),
                                        target)
            except (KeyError, OSError, ValueError, TypeError, AttributeError, tarfile.TarError,
                    config.ConfigError):
                continue
            if loop["id"] == target.stem:
                loops[loop["id"]] = loop
    live, _ = config.readable_loops()
    return list(loops.values()) + [loop for loop in live if loop["id"] not in loops]


def route_problems(entry: dict, live, claims: list) -> list[str]:
    """The fields of an archived route entry that are not what this plugin writes (#783).

    Naming one of the plugin's scripts is not enough to be trusted: ``prompt`` is the observer's
    message, ``deliver``/``deliver_extra`` its destination, and ``profile``, ``host`` and
    ``events`` decide who is woken. ``claims`` is ``[(loop, role)]``, the restored loops that own
    the route's name; the entry must honour one of them (``gate_shims.contract_drift`` plus the
    seat's profile and delivery). A route no loop claims is held to what needs no config: its
    script's prompt, no delivery-only mode, not switched off, no extra destination — and an
    observer route nobody claims is refused, since its destination cannot be checked. A seat's
    ``deliver``/``deliver_extra`` may also equal the live route's own (a restore onto the install
    the backup came from keeps an operator's choice). Returns [] when the entry is fine.
    """
    live = live if isinstance(live, dict) else {}
    script = entry.get("script")
    if not claims:
        if script == route_intent.GATE_SCRIPT["observer"]:
            return ["no restored loop owns this observer route, so its destination cannot be checked"]
        claims = [({}, role) for role in route_intent.GATE_SCRIPT
                  if script == route_intent.GATE_SCRIPT[role]
                  or script in config.LEGACY_GATE_SCRIPTS.get(role, ())]
    best: list[str] | None = None
    for loop, role in claims:
        if script != route_intent.GATE_SCRIPT[role] \
                and script not in config.LEGACY_GATE_SCRIPTS.get(role, ()):
            wrong = ["script"]
        elif loop:
            wrong = gate_shims.contract_drift(loop, role, entry)
            profile = config.seat_profile(loop, role)
            if profile and routes.route_profile(entry) != profile:
                wrong.append("profile")
        else:
            wrong = [key for key, bad in (
                ("prompt", entry.get("prompt") != route_intent.ROUTE_PROMPT[role]),
                ("deliver_only", bool(entry.get("deliver_only"))),
                ("enabled", entry.get("enabled", True) is False)) if bad]
        if role not in ("observer", "observer_urgent"):
            adjudicator = (loop.get("adjudicator") or {}) if loop else {}
            written = (adjudicator.get("deliver", "telegram") if role == "adjudicator"
                       else "discord")
            # "discord" is what every seat route is written with (and what older adjudicator
            # routes carry); "telegram" is the adjudicator's config default.
            allowed = {"discord", written, live.get("deliver")} | (
                set() if loop else {"telegram"})
            if entry.get("deliver") not in allowed:
                wrong.append("deliver")
            if entry.get("deliver_extra") and entry["deliver_extra"] != live.get("deliver_extra"):
                wrong.append("deliver_extra")
        if not wrong:
            return []                    # one claim it honours is enough
        best = sorted(set(wrong)) if best is None else best
    return [f"{', '.join(best or [])} not what this plugin writes for this route"]


def unsafe(manifest: dict, *, allowed_state_dirs=(), archive=None) -> list[str]:
    """Why this archive must not be restored here, or [] (#496 hardening).

    An archive is input from outside: a tampered one must not write outside what this plugin
    owns, install a route that runs anything but this plugin's gates, or replace a route that is
    someone else's. Every file must land inside the Hermes home or the loop files' directory,
    with no symlink in its path; every route must be one of this plugin's gates *and* carry the fields this
    plugin writes for it (prompt, events, profile, host, delivery: ``route_problems``); a live route of
    the same name that is not this plugin's is never overwritten, even with --force.

    A loop's ``state_dir`` may lie elsewhere. The archive declares it, and the archive is not
    trusted (#587): such a directory is written only when the operator named that exact directory
    (``--allow-state-dir DIR``, after the dry run listed it). Naming a parent does not cover it,
    and a named directory the archive does not declare allows nothing.
    """
    roots = [pathlib.Path(os.path.realpath(r)) for r in (config.home(), config.config_dir())]
    outside = [pathlib.Path(d) for d in outside_state_dirs(manifest)]
    named = {pathlib.Path(os.path.abspath(os.path.expanduser(str(d)))) for d in allowed_state_dirs}
    problems = []
    asked = set()

    def judge(target, parent, linked, what):
        inside = any(parent == root or root in parent.parents for root in roots)
        home_dir = next((d for d in outside if parent == d or d in parent.parents), None)
        if linked or not (inside or home_dir):
            problems.append(f"{target}: outside this plugin's places (the Hermes home, the "
                            "loop files' directory and the archive's state directories) or "
                            "behind a symlink")
        elif not inside and home_dir not in named and home_dir not in asked:
            asked.add(home_dir)  # one notice per state directory, not one per path in it
            problems.append(f"{target}: {what} in a loop state directory outside the Hermes home, "
                            f"named by the archive — check it, then re-run with "
                            f"--allow-state-dir {shlex.quote(str(home_dir))} to write it")

    for entry in manifest["files"]:
        target = pathlib.Path(entry["path"])
        parent = pathlib.Path(os.path.realpath(target.parent))
        linked = os.path.islink(target) or parent != pathlib.Path(os.path.abspath(target.parent))
        judge(target, parent, linked, "a file")
    # put_files() also creates every absolute state directory, with or without member files
    # (#768): the directory itself gets the same rules.
    for raw in manifest.get("state_dirs") or []:
        if not pathlib.PurePath(raw).is_absolute():
            continue
        target = pathlib.Path(os.path.abspath(str(raw)))
        real = pathlib.Path(os.path.realpath(target))
        judge(target, real, real != target, "a directory")
    live = routes.all_routes()
    loops = (archived_loops(pathlib.Path(archive), manifest) if archive
             else config.readable_loops()[0])
    claims: dict = {}
    for loop in loops:
        for role, route_name in route_intent.routes_of(loop).items():
            claims.setdefault(route_name, []).append((loop, role))
    for name, entry in (manifest.get("routes") or {}).items():
        if not isinstance(entry, dict) or not route_intent.owned(entry):
            problems.append(f"route {name}: not one of this plugin's gates")
        elif name in live and isinstance(live[name], dict) and not route_intent.owned(live[name]):
            problems.append(f"route {name}: a live route of that name belongs to something else")
        else:
            problems += [f"route {name}: {why}"
                         for why in route_problems(entry, live.get(name), claims.get(name, []))]
    return problems


def existing(manifest: dict) -> list[str]:
    """What a restore would overwrite: files, route entries and the watchdog job."""
    found = [e["path"] for e in manifest["files"] if os.path.lexists(e["path"])]
    live = routes.all_routes()
    found += [f"route {name}" for name in manifest.get("routes") or {} if name in live]
    if manifest.get("cron"):
        if watchdog_job():
            found.append("the watchdog cron job")
    return found


def missing_tokens(manifest: dict) -> list[str]:
    out = []
    for loop_id, tokens in sorted((manifest.get("tokens") or {}).items()):
        for login, path in sorted(tokens.items()):
            problem = credentials.token_file_problem(path)
            if problem:
                out.append(f"{loop_id}: token file for {login} ({path}): {problem}")
    return out


def _put(data, target: pathlib.Path, *, ledger: bool) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if ledger:
        for side in ("-wal", "-shm", "-journal"):
            pathlib.Path(str(target) + side).unlink(missing_ok=True)
    tmp = target.with_name(f".{target.name}.restore-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            while chunk := data.read(1 << 20):
                stream.write(chunk)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def put_files(archive: pathlib.Path, manifest: dict) -> int:
    with tarfile.open(archive, "r:gz") as tar:
        for entry in manifest["files"]:
            member = tar.getmember(entry["member"])
            if not member.isfile():
                raise BackupError(f"{archive}: {entry['member']} is not a regular file")
            _put(tar.extractfile(member), pathlib.Path(entry["path"]),
                 ledger=entry.get("kind") == "ledger")
    for directory in manifest.get("state_dirs") or []:
        if pathlib.PurePath(directory).is_absolute():
            pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
    return len(manifest["files"])
