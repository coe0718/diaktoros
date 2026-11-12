"""``hermes dk backup`` / ``hermes dk restore`` (#496), and the backup ``migrate`` takes first.

One gzip tarball holds everything this plugin owns: the loop files, the runtime file, pacing and
the gate-failure ledger, the run ledger (through SQLite's online backup, so it is consistent
while the loop is live), each loop's state directory, this plugin's route-registry entries, and
the watchdog job's schedule and delivery target.

What the archive holds is a ``manifest.json`` (where every member goes back to, the routes, the
job) plus the members. Route entries carry their HMAC secrets so restored hooks keep working, so
the archive is created 0600 and nothing in it is ever printed. Left out: PATs, GitHub App private
keys, Hermes profiles and model logins. The manifest records the path of each credential file a
loop names; ``restore`` checks each before it writes and lists any that are missing or not
private as ``NOT FINISHED``, but it does not refuse on them: the write goes ahead and the notice
is printed with the final report.

Writer scaffolding is deliberately left out too (#1060, #1694): the plugin's own lock files
(an *empty* file directly in a state directory with a name a writer registered, ``_LOCK_NAMES``;
a lock-named file holding data is archived) and in-progress temp files (``util.atomic_write``'s
name shape *and* held open by their writer), so ``restore`` never writes them back. A listed file
that is gone by the time it is read (a temp renamed into place, an ``artifacts/<n>/`` file
cleaned up) is left out as well, not a failed backup. The manifest's ``skipped`` names every temp
and vanished file left out (not the empty locks), and ``backup`` and ``migrate`` print them
(``cli.lifecycle._report_left_out``).

Retention (#1079): after a successful write to the default place, ``create`` deletes the oldest
archives it wrote there (``$HERMES_HOME/backups``) beyond the newest ``keep`` (``DEFAULT_KEEP``;
``hermes dk backup --keep N``; 0 keeps every archive). A candidate is a regular, non-symlink file
named like ``default_out``'s that carries ``DEFAULT_MARK`` as its first member (written only on a
write to the default place, so a file made by ``--out`` is never one, even with a default-shaped
name inside that directory) and that no run still holds open (each run takes ``util.hold_temp`` on
its archive before the first byte and keeps it until the command that made it is done with the
archive: after the write, the chmod, the prune, and for ``backup`` and ``migrate`` until they
return; see ``create``'s ``using``). Anything else in the directory is neither counted nor deleted.
Archives written before the mark existed carry none, so they are never pruned (delete them by
hand once). ``hermes dk migrate`` takes its backup through ``create`` too, so it prunes with the
default ``keep``. On a filesystem without ``flock`` the hold does not take (``util.hold_temp``
says the same), and an archive another run is still writing is then not kept out by the lock.

Retention is a flag with a built-in default, not a plugin setting. A setting would have to be
settable from the form, ``init``, ``setup``, ``set`` and the loop file, and the form's keys are
per-loop while a backup is per-install; ``--keep`` is per run, so a cron job that runs the backup
has to pass it to get a count other than ``DEFAULT_KEEP``. An output directory still has no knob.
For the same reason ``doctor``'s ``backup:fresh`` check (#1081) warns on a fixed age,
``FRESH_DAYS``, and reads only ``backups_dir()``: it cannot see an archive written with ``--out``
elsewhere, nor a backup taken some other way. It does not trust an archive dated more than
``CLOCK_SKEW_S`` in the future, since that file would stay "fresh" long after the backup. Because
that cutoff moves with the clock, ``hermes dk backup`` records each such archive it sees in
``FUTURE_RECORD`` (``record_future_dated``, #1905) and ``doctor`` keeps a recorded one apart after
the clock reaches its date; one that arrives and is caught up with between two backup runs is not
recorded.
"""
from __future__ import annotations

import contextlib
import gzip
import io
import importlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
import zlib

from .ledger import LOCK_WAIT_S, read_only
from .config import credentials
from . import config, route_intent, routes, run_supervisor, util

VERSION = 1
MANIFEST = "manifest.json"
FRESH_DAYS = 30         # doctor's backup:fresh warns when the newest archive is older than this
CLOCK_SKEW_S = 86400    # an archive dated further ahead than this is not trusted as recent (#1875)
WARNING = ("this archive holds route secrets (webhook HMAC keys): keep it private; "
           "it was written mode 0600 and its contents are never printed")


_DEFAULT_TRIES = 20     # re-picks of a default name that a concurrent run took first
DEFAULT_KEEP = 10       # default-named archives kept in the default directory; 0 keeps them all
_DEFAULT_NAME = re.compile(r"diaktoros-backup-(\d{8}-\d{6})(?:-(\d+))?\.tar\.gz")
# The first member of an archive ``create`` wrote to the default place: its provenance (#1079).
# ``prune`` deletes only archives that carry it, so a default-shaped file that came from ``--out``,
# or from a version before the mark, is never a candidate, whatever its name or directory.
DEFAULT_MARK = "default-place"


class BackupError(RuntimeError):
    pass


def backups_dir() -> pathlib.Path:
    """Where ``create`` puts a default-named archive, and the one place ``doctor``'s
    ``backup:fresh`` check looks for one (#1081)."""
    return config.home() / "backups"


def latest_archive(now: float | None = None) -> tuple[tuple[pathlib.Path, float] | None,
                                                      tuple[pathlib.Path, float] | None]:
    """The archives in ``backups_dir()`` as ``(newest, ahead)``, each ``(path, mtime)`` or None.

    ``newest`` is the most recently written archive whose mtime is not past ``now`` plus
    ``CLOCK_SKEW_S`` (``now`` defaults to the current time) and that ``hermes dk backup`` has not
    recorded as future-dated (``record_future_dated``). ``ahead`` is the latest-dated archive
    that is either: written while the host clock was wrong, or copied in from a host whose
    clock is. Its age cannot be told, and trusting it would keep ``doctor`` green for as long as
    the clock was ahead plus ``FRESH_DAYS``, however long ago the backup really was (#1875); a
    recorded one stays apart after the clock reaches its date (#1905). So the
    caller must not count it as a recent backup. It is returned so that an install holding nothing
    else can be told why, rather than reported as having no archive.

    An archive here is a regular, non-empty ``*.tar.gz`` (not a symlink), whatever its name: an
    operator's ``--out`` into this directory counts as much as a default name. The file is not
    opened, so this says a recent archive *exists*, not that it would restore. A directory that
    does not exist is "none"; one that cannot be read raises ``OSError`` — the caller says so
    rather than reporting a backup that may be there as missing. So does a damaged
    ``FUTURE_RECORD`` (``FutureRecordError``, an ``OSError``): it is never read as empty.
    """
    limit = (time.time() if now is None else now) + CLOCK_SKEW_S
    try:
        entries = _archives()
    except FileNotFoundError:
        return None, None
    recorded = _read_future_record()
    newest: tuple[pathlib.Path, float] | None = None
    ahead: tuple[pathlib.Path, float] | None = None
    for path, mtime in entries:
        found = (path, mtime)
        # Past the allowance now, or seen past it by an earlier ``create`` (#1905): the clock
        # catching up with the file's date does not make that date its age.
        if mtime > limit or recorded.get(path.name) == mtime:
            ahead = found if ahead is None or found[1] > ahead[1] else ahead
        elif newest is None or mtime > newest[1]:
            newest = found
    return newest, ahead


def _archives() -> list[tuple[pathlib.Path, float]]:
    """Every archive in ``backups_dir()`` as ``(path, mtime)``: a regular, non-empty, non-symlink
    ``*.tar.gz``. Raises ``FileNotFoundError`` when the directory does not exist and ``OSError``
    when it cannot be read; a file gone between the listing and its ``stat`` is skipped."""
    found = []
    for entry in os.scandir(backups_dir()):
        if not entry.name.endswith(".tar.gz") or not entry.is_file(follow_symlinks=False):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue        # gone between the listing and now
        if info.st_size:
            found.append((pathlib.Path(entry.path), info.st_mtime))
    return found


def _free_name(prefix: str) -> pathlib.Path:
    """``backups_dir()/<prefix>-<stamp>.tar.gz``, with ``-2``, ``-3`` ... appended to the stamp
    while that name is taken: two runs in one second must not collide."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = backups_dir()
    path = base / f"{prefix}-{stamp}.tar.gz"
    n = 1
    while path.exists() or path.is_symlink():
        n += 1
        path = base / f"{prefix}-{stamp}-{n}.tar.gz"
    return path


def default_out() -> pathlib.Path:
    return _free_name("diaktoros-backup")


def pre_restore_out() -> pathlib.Path:
    """Where ``restore`` writes its snapshot of the live install before it overwrites anything
    (#1049): in ``backups_dir()``, but not named like ``default_out`` and written as an ``--out``
    archive (no ``DEFAULT_MARK``), so ``prune`` never counts or deletes it and taking it never
    prunes another archive (which could be the one being restored)."""
    return _free_name("diaktoros-pre-restore")


def check_keep(keep: int) -> None:
    """``--keep`` is a count of archives: 0 (keep all) or more."""
    if keep < 0:
        raise BackupError(f"--keep must be 0 (keep all) or more, not {keep}")


def made_by_default(path: pathlib.Path) -> bool:
    """Did ``create`` write the archive at ``path`` to the default place? Its first member says so
    (``DEFAULT_MARK``); only that header is read. An archive from ``--out`` or from before the
    mark, and a file that is not a readable tar.gz, is not."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            first = tar.next()
    except (OSError, EOFError, zlib.error, tarfile.TarError):
        return False
    return first is not None and first.name == DEFAULT_MARK


def prune(keep: int, protect: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Delete the oldest default-place archives in the default directory beyond the newest ``keep``.

    A candidate is a regular, non-symlink file whose name is one ``default_out`` makes, that
    carries ``DEFAULT_MARK`` (``made_by_default``: so not an ``--out`` archive that happens to be
    named alike), and that no writer still holds open (``util.temp_in_use``: ``create`` holds a
    lock on its file until it is whole, and its caller may keep holding it while it uses the
    path, so another run's archive in either state is neither counted nor deleted). Anything
    else in the directory is left alone. Candidates are ordered by the
    stamp in the name and then its collision suffix (not mtime, which a copy resets).
    ``protect`` (the archive just written) is never deleted. ``keep`` 0 deletes nothing. A file
    that cannot be removed is left: retention never fails a backup that was written. Returns what
    was deleted.
    """
    check_keep(keep)
    base = backups_dir()
    if not keep or not base.is_dir():
        return []
    mine = protect.absolute() if protect is not None else None
    found = []
    for path in base.iterdir():
        match = _DEFAULT_NAME.fullmatch(path.name)
        # ``protect`` is counted but never skipped as held: the run that wrote it holds it (see
        # ``create``'s ``using``) and it is the newest archive that run made.
        if match and path.is_file() and not path.is_symlink() and made_by_default(path) \
                and (path.absolute() == mine or not util.temp_in_use(path)):
            # Mark first, then the lock: ``create`` takes its lock before it writes a byte, so a
            # readable mark means the lock is already held or the archive is whole. The other
            # order could see "unlocked" just before a run's lock and its first flushed bytes.
            found.append(((match[1], int(match[2] or 1)), path))
    found.sort(key=lambda item: item[0], reverse=True)
    removed = []
    for _, path in found[keep:]:
        if path.absolute() == mine:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(path)
    return removed


# The flock files the plugin's own writers keep directly in a state directory (or the gate-failure
# directory). Each writer registers its lock where it names it (``util.register_lock``). The
# registrations happen when a writer's module is imported, and a backup process imports few of
# them, so ``_load_writers`` imports every module of the package whose source calls
# ``register_lock(``: found by that call, not by a list here, so a new writer needs nothing else.
# Exact names, not a ``*.lock`` pattern: a user's ``user.lock`` is state (#1060).
def _load_writers() -> frozenset[str]:
    root = pathlib.Path(__file__).resolve().parent
    for path in sorted(root.rglob("*.py")):
        parts = path.relative_to(root).with_suffix("").parts
        if parts[-1] == "__main__" or "register_lock(" not in path.read_text(errors="replace"):
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts and parts != ("util",) and parts != ("backup",):
            importlib.import_module("." + ".".join(parts), __package__)
    return frozenset(util.LOCK_NAMES)


_LOCK_NAMES = _load_writers()


def _transient(path: pathlib.Path, root: pathlib.Path) -> bool:
    """Is ``path`` writer scaffolding that a restore would write back as litter (#1060)?

    Only what is known to be scaffolding, and only where the writers put it: directly in the
    directory they own (``root``), never below it (``artifacts/<n>/`` holds a loop's work product,
    which may legitimately contain any name):
    - an *empty* file with one of the plugin's own lock names (``_LOCK_NAMES``): flock files never
      hold data. Any other ``*.lock`` (``Cargo.lock``, ``user.lock``) is state and is kept;
    - a temp file a writer holds right now: a name of ``util.atomic_write``'s shape
      (``.<name>.<8 chars>.dktmp``) *and* an ``flock`` held on the file (``util.hold_temp``, taken
      for as long as the writer has it open). A first write has no target beside it and is still
      recognised. The name alone proves nothing: a user's file of that name, or a crashed
      writer's leftover, nobody holds, and it is archived, never dropped (#1651).
    What is left out is never silent: ``_regular_files`` reports a temp and the manifest lists it."""
    if path.parent != root:
        return False
    if path.name in _LOCK_NAMES:
        try:
            return path.stat().st_size == 0
        except OSError:
            return True      # gone between the listing and now: nothing to archive
    return util.is_temp_name(path.name) and util.temp_in_use(path)


def _regular_files(root: pathlib.Path, skipped: list[str] | None = None) -> list[pathlib.Path]:
    """The regular files to archive under ``root``. Each temp-shaped name left out is appended to
    ``skipped`` when given, so the archive says what it did not take."""
    if root.is_symlink() or not root.exists():
        return []
    if root.is_file():
        return [root]
    from . import app_tokens
    shared = app_tokens.directory().resolve()
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.resolve().is_relative_to(shared):
            continue        # live App tokens (#1780), even under a state_dir set to state/ itself
        if _transient(path, root):
            if skipped is not None and util.is_temp_name(path.name):
                skipped.append(str(path))
            continue
        found.append(path)
    return found


def _snapshot_ledger(src: pathlib.Path, dest: pathlib.Path) -> None:
    """SQLite's online backup: a consistent copy while other connections write."""
    source = read_only(src)
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
    """Every identity's credential path: PAT files and GitHub App private keys (#1163). Paths
    only, never contents: the archive does not hold the files. ``restore`` does check them
    before it writes: ``missing_tokens`` runs ``credentials.token_file_problem`` on each path in
    the manifest. The check only reports, it never refuses (``restore_checks.unsafe``, which can refuse, does not
    read the manifest's tokens). A missing or non-private file does not stop the write: the
    archive is restored in full, and the notices are printed as ``NOT FINISHED`` in the final
    report (exit 1; a dry run prints them too and exits 0), so the files can be put back
    afterwards."""
    return {str(login): str(path) for login, path in config.credentials_map(loop).items() if path}


def _open_new(out: pathlib.Path, chosen: bool) -> tuple[int, pathlib.Path]:
    """Create the archive file exclusively (0600) and return ``(fd, path)``. A default name that
    another run took after ``default_out`` looked is re-picked, up to ``_DEFAULT_TRIES`` times; an
    ``--out`` that exists is refused. The new file is locked (``util.hold_temp``) before anything
    is written and until the caller closes ``fd``, so ``prune`` in another run skips an archive
    that is not whole yet (#1079)."""
    for attempt in range(_DEFAULT_TRIES):
        try:
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            if chosen:
                raise BackupError(f"{out} already exists — pick another --out") from None
            # A default name another run took after default_out() looked: pick the next free one.
            if attempt < _DEFAULT_TRIES - 1:
                out = default_out().absolute()
    else:
        # Every try was taken: the loop's own bound gives up, so ``fd`` is bound past here (#1694).
        raise BackupError(f"{out} already exists and no free default name was found; "
                          "retry, or pass --out")
    util.hold_temp(fd)
    return fd, out


def _listed_files(loops: list[dict]) -> tuple[list[tuple[pathlib.Path, pathlib.Path | None, str]],
                                              list[str]]:
    """The files ``create`` archives, as ``(target, source, kind)``, and the temp-shaped names it
    leaves out (``skipped``; the manifest lists them, with any listed file that is gone by the
    time ``create`` reads it): the loop files, the runtime and pacing
    files, the gate-failure ledger and each loop's state directory. The ledger is added later."""
    files: list[tuple[pathlib.Path, pathlib.Path | None, str]] = []
    cfg = config.config_dir()
    for path in sorted(cfg.glob("*.json")) if cfg.is_dir() else []:
        if path.is_file() and not path.is_symlink():
            files.append((path, path, "file"))
    for key in ("runtime", "pacing"):
        path = config.host_path(key)
        if path.is_file() and not path.is_symlink():
            files.append((path, path, "file"))
    skipped: list[str] = []
    for key in ("gate_failures",):
        for path in _regular_files(config.host_path(key), skipped):
            files.append((path, path, "file"))
    for loop in loops:
        for path in _regular_files(config.state_dir(loop), skipped):
            files.append((path, path, "file"))
    return files, skipped


def _manifest(loops: list[dict], skipped: list[str], half_moved: dict | None) -> dict:
    """``create``'s manifest before any member is added; ``migrating`` is ``half_moved``."""
    return {"version": VERSION, "created": time.time(), "loops": [l["id"] for l in loops],
            "files": [], "skipped": skipped,
            "state_dirs": sorted({str(config.state_dir(l)) for l in loops}),
            "routes": {name: entry for name, entry in routes.all_routes().items()
                       if isinstance(entry, dict) and route_intent.owned(entry)},
            "cron": watchdog_job(),
            "tokens": {l["id"]: token_paths(l) for l in loops}, "migrating": half_moved}


def _path_key(path: str | os.PathLike) -> str:
    """The one spelling ``create`` compares a listed path and a ``must_read`` path by (#2413):
    the real path of the parent directory plus the normalized last name, so ``/./``, repeated
    slashes, a leading ``//`` and a symlinked directory on the way all name the same entry. The
    last name itself is not followed: the entry may be gone, or a symlink, when this is asked.
    A restore archive's manifest may spell a target any way ``read_manifest`` accepts; the
    listing spells it as ``pathlib`` does."""
    head, name = os.path.split(os.path.normpath(os.fspath(path)))
    return os.path.join(os.path.realpath(head), name)


def _add_files(tar: tarfile.TarFile, files: list[tuple[pathlib.Path, pathlib.Path | None, str]],
               manifest: dict, must_read: frozenset[str]) -> None:
    """Add each listed ``(target, source, kind)`` to ``tar`` as ``files/<index>`` and record it
    in ``manifest["files"]``. A listed regular file gone by the read is named in ``skipped``
    instead (#1694), unless it is a ``must_read`` path (#2413; both compared by ``_path_key``);
    any other read error, and a vanished ledger snapshot, raises ``BackupError``."""
    must_read = frozenset(_path_key(path) for path in must_read)
    for index, (target, source, kind) in enumerate(files):
        member = f"files/{index}"
        try:
            tar.add(source, arcname=member, recursive=False)
        except FileNotFoundError as exc:
            if kind == "file" and _path_key(target) not in must_read:
                # Removed or renamed away since the listing (a temp renamed into place,
                # an ``artifacts/<n>/`` file cleaned up): there is nothing left to archive,
                # so it is left out and named, not a failed backup (#1694).
                manifest["skipped"].append(str(target))
                continue
            raise BackupError(f"cannot read {source}: {exc}") from exc
        except OSError as exc:
            raise BackupError(f"cannot read {source}: {exc}") from exc
        manifest["files"].append({"member": member, "path": str(target), "kind": kind})


def create(out: pathlib.Path | None = None, keep: int = DEFAULT_KEEP,
           pruned: list[pathlib.Path] | None = None,
           using: contextlib.ExitStack | None = None, *, held: bool = False,
           half_moved: dict | None = None,
           must_read: frozenset[str] = frozenset()) -> pathlib.Path:
    """Write the archive and return its path. Refuses to overwrite a file.

    A default-named archive (no ``out``) is followed by ``prune(keep)``; what it deleted is
    appended to ``pruned`` when given. An ``out`` archive is never followed by a prune.

    The archive is locked from before its first byte (``_open_new``) until ``create`` returns, so
    no other run's ``prune`` can take it while it is written, chmodded or pruned around. With
    ``using`` (an ``ExitStack`` the caller owns) the lock is kept until the caller closes the
    stack, so a caller that goes on to read or print the archive has it for as long as it needs
    it; without, the lock ends when ``create`` returns.

    Refused while ``migrate``'s marker exists (#1054, ``migrate.snapshot_refusal``), unless the
    caller holds it (``held``: ``migrate``'s own backup), which passes an interrupted run's
    marker as ``half_moved``: ``restore`` puts it back.

    A listed file that is gone by the time it is read is left out and named in ``skipped``
    (#1694), unless its path is in ``must_read``: then the backup fails with ``BackupError``.
    ``restore``'s undo snapshot passes every path it is about to write, so a file removed (and
    perhaps recreated) while the snapshot reads it is never overwritten without being in it."""
    check_keep(keep)
    from . import migrate  # noqa: PLC0415 - the pause marker's owner
    if not held and migrate.snapshot_refusal():
        raise BackupError(migrate.snapshot_refusal())
    chosen = bool(out)
    out = (pathlib.Path(out).expanduser() if chosen else default_out()).absolute()
    loops = config.all_loops()
    files, skipped = _listed_files(loops)
    ledger = run_supervisor.production_ledger()
    manifest = _manifest(loops, skipped, half_moved)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, out = _open_new(out, chosen)
    try:
        # ``closefd=False``: the lock is on ``fd`` and must outlive the stream (see ``using``).
        with os.fdopen(fd, "wb", closefd=False) as stream, \
                tarfile.open(fileobj=stream, mode="w:gz") as tar, \
                tempfile.TemporaryDirectory() as scratch:
            if not chosen:
                tar.addfile(tarfile.TarInfo(DEFAULT_MARK))   # provenance: first member, see prune
            if ledger.is_file():
                snap = pathlib.Path(scratch) / "ledger"
                _snapshot_ledger(ledger, snap)
                files.append((ledger, snap, "ledger"))
            _add_files(tar, files, manifest, must_read)
            raw = json.dumps(manifest, indent=1).encode()
            info = tarfile.TarInfo(MANIFEST)
            info.size, info.mtime, info.mode = len(raw), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(raw))
        os.chmod(out, 0o600)           # still locked: no other run's prune can take it from here
    except BaseException:
        try:
            out.unlink(missing_ok=True)
        finally:
            os.close(fd)
        raise
    with contextlib.ExitStack() as own:
        (own if using is None else using).callback(os.close, fd)
        if not chosen:
            removed = prune(keep, protect=out)
            if pruned is not None:
                pruned.extend(removed)
    return out


def read_manifest(archive: pathlib.Path) -> dict:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.getmember(MANIFEST)
            manifest = json.load(tar.extractfile(member))
    except (OSError, EOFError, KeyError, ValueError, tarfile.TarError, zlib.error) as exc:
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


def _ledger_problem(path: pathlib.Path) -> str | None:
    """Why the extracted ledger snapshot at ``path`` is not a usable SQLite database, or None.

    Opened on the caller's private scratch copy, never on the archive or the live ledger."""
    if path.stat().st_size == 0:
        return "the ledger snapshot is empty"
    try:
        con = sqlite3.connect(path, timeout=LOCK_WAIT_S)
        try:
            rows = [row[0] for row in con.execute("PRAGMA integrity_check")]
        finally:
            con.close()
    except sqlite3.Error as exc:
        return f"the ledger snapshot does not open as SQLite ({type(exc).__name__}: {exc})"
    if rows == ["ok"]:
        return None
    return "the ledger snapshot fails SQLite's integrity check: " + "; ".join(rows[:3])


def _read_stream_to_end(archive: pathlib.Path) -> None:
    """Decompress the whole gzip stream of ``archive`` and throw the bytes away.

    ``tarfile`` stops at the tar's own end-of-archive blocks, so the gzip trailer (CRC32 and
    length) is never read by it: an archive cut or damaged after its last member still lists and
    extracts. Reading to the end is what makes ``gzip`` check that trailer. Raises ``EOFError``
    (stream cut short), ``gzip.BadGzipFile`` (an ``OSError``: bad CRC, length or header) or
    ``zlib.error`` (corrupt deflate data)."""
    with gzip.open(archive, "rb") as stream:
        while stream.read(1 << 20):
            pass


def verify(archive: pathlib.Path) -> tuple[int, list[str]]:
    """Read a backup back as a restore would, writing nothing outside a temp directory (#1078).

    Re-reads the manifest (``read_manifest``), reads every member the manifest names to its end,
    reads the whole gzip stream to its end so the trailer is checked too (``_read_stream_to_end``:
    a cut or damaged tail fails here even when every member reads) and opens each ledger snapshot
    with SQLite's integrity check. Returns ``(members read, problems)``; no problems means the
    archive is restorable as far as its bytes go. Whether it is *safe* to restore here is
    ``restore_checks.unsafe``'s question, which ``restore --dry-run`` asks too. A manifest that does not read
    raises ``BackupError``."""
    manifest = read_manifest(archive)
    problems: list[str] = []
    read = 0
    try:
        with tarfile.open(archive, "r:gz") as tar, tempfile.TemporaryDirectory() as scratch:
            for index, entry in enumerate(manifest["files"]):
                try:
                    member = tar.getmember(entry["member"])
                except KeyError:
                    problems.append(f"{entry['path']}: member {entry['member']} is missing")
                    continue
                if not member.isfile():
                    problems.append(f"{entry['path']}: {entry['member']} is not a regular file")
                    continue
                copy = pathlib.Path(scratch) / str(index)
                with open(copy, "wb") as out:
                    shutil.copyfileobj(tar.extractfile(member), out)
                read += 1
                if copy.stat().st_size != member.size:
                    problems.append(f"{entry['path']}: {entry['member']} is truncated")
                elif entry.get("kind") == "ledger":
                    why = _ledger_problem(copy)
                    if why:
                        problems.append(f"{entry['path']}: {why}")
        _read_stream_to_end(archive)
    except (OSError, EOFError, tarfile.TarError, zlib.error) as exc:
        problems.append(f"the archive cannot be read to its end ({type(exc).__name__}: {exc})")
    return read, problems


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
    """Each credential file (a PAT file or an App private key) the manifest names that is
    missing or not private on this host, as ``<loop>: credential file for <login> (<path>):
    <problem>``. Only metadata is inspected. ``restore`` calls this before it writes, but the
    result is notices to print with the final report, not a reason to refuse or to skip the
    write."""
    out = []
    for loop_id, tokens in sorted((manifest.get("tokens") or {}).items()):
        for login, path in sorted(tokens.items()):
            problem = credentials.token_file_problem(path)
            if problem:
                out.append(f"{loop_id}: credential file for {login} ({path}): {problem}")
    return out


def _stage(data, target: pathlib.Path) -> pathlib.Path:
    """Stream one archive member to a private temp file beside ``target`` and return its path.
    Nothing live is touched: ``put_files`` swaps the staged files in only once every member has
    been read and written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.restore-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            while chunk := data.read(1 << 20):
                stream.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _swap_in(staged: list[tuple[pathlib.Path, pathlib.Path, bool]],
             undo: list[tuple[pathlib.Path, pathlib.Path | None]]) -> None:
    """Put each staged file in place, journalling in ``undo`` how to take it back: each live file
    it displaces (and, for the ledger, its ``-wal``/``-shm``/``-journal`` sidecars, which belong
    to the live database, not the restored one) is renamed aside first, as ``(path, aside)``; a
    path that did not exist is ``(path, None)``."""
    for tmp, target, ledger in staged:
        sides = [pathlib.Path(str(target) + s) for s in ("-wal", "-shm", "-journal")] if ledger else []
        for path in [target, *sides]:
            if os.path.lexists(path):
                aside = path.with_name(f".{path.name}.pre-restore-{os.getpid()}")
                os.rename(path, aside)
                undo.append((path, aside))
            else:
                undo.append((path, None))
        os.replace(tmp, target)


def _roll_back(undo: list[tuple[pathlib.Path, pathlib.Path | None]]) -> list[str]:
    """Undo ``_swap_in``'s journal, newest first: each displaced file goes back, each path that
    did not exist before is removed. Returns what could not be put back, as text."""
    left = []
    for path, aside in reversed(undo):
        try:
            if aside is None:
                path.unlink(missing_ok=True)
            else:
                os.replace(aside, path)
        except OSError as exc:
            left.append(f"{path} ({exc}; the original is at {aside})" if aside else f"{path} ({exc})")
    return left


def put_files(archive: pathlib.Path, manifest: dict) -> int:
    """Put the archive's files back, all or none (#1049).

    Every member is first streamed to a temp file beside its target (``_stage``), so a member that
    cannot be read or a full disk fails before anything live is touched. Only then are the staged
    files swapped in (``_swap_in``), each displaced file renamed aside and journalled; if a swap
    fails, the journal is rolled back (``_roll_back``) and the live files are what they were. A
    rollback that itself fails is named in the ``BackupError``, with where each original was left.
    Directories created for new targets are not removed. Then every absolute state directory the
    manifest declares is created."""
    staged: list[tuple[pathlib.Path, pathlib.Path, bool]] = []
    undo: list[tuple[pathlib.Path, pathlib.Path | None]] = []
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for entry in manifest["files"]:
                member = tar.getmember(entry["member"])
                if not member.isfile():
                    raise BackupError(f"{archive}: {entry['member']} is not a regular file")
                target = pathlib.Path(entry["path"])
                staged.append((_stage(tar.extractfile(member), target), target,
                               entry.get("kind") == "ledger"))
        try:
            _swap_in(staged, undo)
        except BaseException as exc:
            left = _roll_back(undo)
            if left:
                raise BackupError(f"restore failed ({exc}) and could not be undone for: "
                                  + "; ".join(left)) from exc
            raise
        for _, aside in undo:
            if aside is not None:
                aside.unlink(missing_ok=True)
    finally:
        for tmp, _, _ in staged:
            tmp.unlink(missing_ok=True)
    for directory in manifest.get("state_dirs") or []:
        if pathlib.PurePath(directory).is_absolute():
            pathlib.Path(directory).mkdir(parents=True, exist_ok=True)
    return len(manifest["files"])


# The future-dated archive record (#1905) lives in ``backup_future`` (size ratchet); re-exported
# here, after every name it reaches back for, so ``backup.<name>`` keeps working for callers.
from .backup_future import (  # noqa: E402
    FUTURE_RECORD, FutureRecordError, _future_record_locked, _future_record_path,
    _read_future_record, _recorded_mtime, record_future_dated)
