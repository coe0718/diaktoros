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
``CLOCK_SKEW_S`` in the future, since that file would stay "fresh" long after the backup.
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
import shlex
import shutil
import sqlite3
import tarfile
import tempfile
import time
import zlib

from .ledger import LOCK_WAIT_S, read_only
from .config import credentials
from . import config, gate_shims, route_intent, routes, run_supervisor, util

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
    ``CLOCK_SKEW_S`` (``now`` defaults to the current time). ``ahead`` is the latest-dated archive
    that is past that: written while the host clock was wrong, or copied in from a host whose
    clock is. Its age cannot be told, and trusting it would keep ``doctor`` green for as long as
    the clock was ahead plus ``FRESH_DAYS``, however long ago the backup really was (#1875), so the
    caller must not count it as a recent backup. It is returned so that an install holding nothing
    else can be told why, rather than reported as having no archive.

    An archive here is a regular, non-empty ``*.tar.gz`` (not a symlink), whatever its name: an
    operator's ``--out`` into this directory counts as much as a default name. The file is not
    opened, so this says a recent archive *exists*, not that it would restore. A directory that
    does not exist is "none"; one that cannot be read raises ``OSError`` — the caller says so
    rather than reporting a backup that may be there as missing.
    """
    limit = (time.time() if now is None else now) + CLOCK_SKEW_S
    try:
        entries = list(os.scandir(backups_dir()))
    except FileNotFoundError:
        return None, None
    newest: tuple[pathlib.Path, float] | None = None
    ahead: tuple[pathlib.Path, float] | None = None
    for entry in entries:
        if not entry.name.endswith(".tar.gz") or not entry.is_file(follow_symlinks=False):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue        # gone between the listing and now
        if not info.st_size:
            continue
        found = (pathlib.Path(entry.path), info.st_mtime)
        if info.st_mtime > limit:
            ahead = found if ahead is None or found[1] > ahead[1] else ahead
        elif newest is None or info.st_mtime > newest[1]:
            newest = found
    return newest, ahead


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
    the manifest. The check only reports, it never refuses (``unsafe``, which can refuse, does not
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
            if attempt == _DEFAULT_TRIES - 1:
                raise BackupError(f"{out} already exists and no free default name was found; "
                                  "retry, or pass --out") from None
            out = default_out().absolute()
    util.hold_temp(fd)
    return fd, out


def _listed_files(loops: list[dict]) -> tuple[list[tuple[pathlib.Path, pathlib.Path | None, str]],
                                              list[str]]:
    """The files ``create`` archives, as ``(target, source, kind)``, and the temp-shaped names it
    leaves out (``skipped``; the manifest lists them): the loop files, the runtime and pacing
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


def create(out: pathlib.Path | None = None, keep: int = DEFAULT_KEEP,
           pruned: list[pathlib.Path] | None = None,
           using: contextlib.ExitStack | None = None, *, held: bool = False,
           half_moved: dict | None = None) -> pathlib.Path:
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
    marker as ``half_moved``: ``restore`` puts it back."""
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
            for index, (target, source, kind) in enumerate(files):
                member = f"files/{index}"
                try:
                    tar.add(source, arcname=member, recursive=False)
                except FileNotFoundError as exc:
                    if kind == "file" and util.is_temp_name(target.name):
                        manifest["skipped"].append(str(target))   # renamed away since the listing
                        continue
                    raise BackupError(f"cannot read {source}: {exc}") from exc
                except OSError as exc:
                    raise BackupError(f"cannot read {source}: {exc}") from exc
                manifest["files"].append({"member": member, "path": str(target), "kind": kind})
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
    ``unsafe``'s question, which ``restore --dry-run`` asks too. A manifest that does not read
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
    load is left out, so the routes it would have claimed are judged as unclaimed (stricter) —
    and it still replaces the live loop file of that name, so that loop's claims do not count
    either (review on #917: a broken archived file borrowed the live loop's routes)."""
    loops: dict = {}
    replaced: set[str] = set()
    with tarfile.open(archive, "r:gz") as tar:
        for entry in manifest["files"]:
            target = pathlib.Path(entry["path"])
            if target.suffix != ".json" or target.parent != config.config_dir():
                continue
            replaced.add(target.stem)        # a live loop's id is its file's stem
            try:
                loop = config.normalize(json.load(tar.extractfile(tar.getmember(entry["member"]))),
                                        target)
            except (KeyError, OSError, ValueError, TypeError, AttributeError, tarfile.TarError,
                    config.ConfigError):
                continue
            if loop["id"] == target.stem:
                loops[loop["id"]] = loop
    live, _ = config.readable_loops()
    return list(loops.values()) + [loop for loop in live if loop["id"] not in replaced]


def secret_problems(entry: dict, live, claims: list) -> list[str]:
    """An archived HMAC secret that is not the one this install holds for the route (#920).

    The live route's secret is the reference; with no live route, the claiming loops' intent
    record (``route_intent.load``) is. A restore onto a machine with neither has nothing to
    compare against, so the archive's secret is taken as given there. Returns [] when it matches
    or there is no reference.
    """
    known = []
    if isinstance(live, dict) and live.get("secret"):
        known.append(live["secret"])
    else:
        for loop, role in claims:
            name = route_intent.routes_of(loop).get(role) if loop else None
            try:
                recorded = (route_intent.load(loop) or {}).get(name) if loop else None
            except route_intent.IntentError:
                continue
            if isinstance(recorded, dict) and recorded.get("secret"):
                known.append(recorded["secret"])
    if known and entry.get("secret") not in known:
        return ["secret differs from the one this install holds for the route"]
    return []


def route_problems(entry: dict, live, claims: list) -> list[str]:
    """``secret_problems`` plus ``field_problems``: everything wrong with an archived route."""
    return secret_problems(entry, live, claims) + field_problems(entry, live, claims)


def field_problems(entry: dict, live, claims: list) -> list[str]:
    """The fields of an archived route entry that are not what this plugin writes (#783).

    Naming one of the plugin's scripts is not enough to be trusted: ``prompt`` is the observer's
    message, ``deliver``/``deliver_extra`` its destination, and ``profile``, ``host`` and
    ``events`` decide who is woken. ``claims`` is ``[(loop, role)]``, the restored loops that own
    the route's name; the entry must honour one of them (``gate_shims.contract_drift`` plus the
    seat's profile and delivery). A route no loop claims is held to what needs no config: its
    script's prompt, no delivery-only mode, not switched off, no extra destination — and its
    ``profile``, ``host`` and ``events`` must be the live route's of that name (a restore onto the
    install it came from), since no config says what they should be (review on #917). An
    unclaimed route with no live route to match, and an unclaimed observer route (its destination
    cannot be checked), are refused. A seat's
    ``deliver``/``deliver_extra`` may also equal the live route's own (a restore onto the install
    the backup came from keeps an operator's choice). Returns [] when the entry is fine.
    """
    live = live if isinstance(live, dict) else {}
    script = entry.get("script")
    if not claims:
        if script == route_intent.GATE_SCRIPT["observer"]:
            return ["no restored loop owns this observer route, so its destination cannot be checked"]
        if not live:
            return ["no restored loop owns this route and none is live under its name, so its "
                    "profile, host and events cannot be checked"]
        differ = [key for key in ("profile", "host", "events") if key == "profile"
                  and routes.route_profile(entry) != routes.route_profile(live)
                  or key != "profile" and entry.get(key) != live.get(key)]
        if differ:
            return [f"no restored loop owns this route, and its {', '.join(differ)} differ from "
                    "the live route's"]
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
    plugin writes for it (prompt, events, profile, host, delivery: ``route_problems``, which also refuses a secret differing from this install's, #920); a live route of
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
