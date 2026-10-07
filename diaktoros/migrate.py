"""One-time move from the ``hermes-review-loop`` plugin to its new name (#425).

Hermes knows a plugin by its manifest ``name``: the install folder, ``plugins.enabled`` and
``plugins.entries.<name>.settings`` are all keyed by it. A renamed plugin is therefore a new
plugin to Hermes: it starts with a blank settings form, and the gate and watchdog shims still run
the old folder's scripts. A renamed GitHub repository is the same story for the loop: the loop
file, the run ledger and the loop's state all name the repository, and a webhook from the
renamed repository matches no loop at all.

``migrate`` closes those gaps, in an order that a crash or a re-run cannot hurt, while the
install is paused (below):

1. **settings** — every setting the old plugin's form holds and this plugin's form does not is
   copied, through Hermes's own settings writer (``ctx.set_config``). A value already set here is
   never overwritten.
2. **repository** — a loop whose repository GitHub now reports under another name (the same
   repository id, read twice) has every record moved to that name: the run ledger in one
   transaction, the loop's state files under its locks, and the loop file last, so a re-run
   finishes whatever an interrupted one began. Refused while a run is in flight or uncertain.
3. **shims** — the gate shims and the watchdog shim are pointed at this plugin's scripts.
4. **doctor** — every check that is not verified is listed.

``--dry-run`` reports each step and writes nothing.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import pathlib
import re
import sqlite3
import time

from . import config, gh, gate_shims, state as state_mod
from . import envnames

OLD_PLUGIN = "hermes-review-loop"


# -- the pause (#431) ------------------------------------------------------------------------
# While a migration moves records from one name to another, nothing may write under either: a
# gate that wrote between the ledger move and the loop file would leave rows a re-run never
# finds. So ``migrate`` holds a host-wide marker for its whole run, and while it exists every
# gate records its delivery for a later re-drive instead of acting (``gate_failures.run``), the
# worker claims no run, and the watchdog sweeps nothing. A marker left by a migrate that died
# keeps the loop paused — its records may be half moved — until ``migrate`` runs again.

MARKER_FILE = "migrating.json"


class MigrationBusy(RuntimeError):
    """Another migrate is running now."""


def marker_path() -> pathlib.Path:
    return config.home() / "state" / MARKER_FILE


def migrating() -> dict | None:
    """The marker's facts while a migration holds (or held) the install; ``None`` otherwise.

    An unreadable marker still pauses: it says a migration started, and not who or when."""
    path = marker_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError:
        return {"unreadable": True}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"unreadable": True}
    return data if isinstance(data, dict) else {"unreadable": True}


def lock_path() -> pathlib.Path:
    return marker_path().with_suffix(".lock")


def _running() -> bool:
    """Whether a migrate holds the lock right now (#447): the lock, not a PID, says so — two
    runs cannot both take it, and a reused PID cannot pass for a live one."""
    try:
        fd = os.open(lock_path(), os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)             # closing releases a lock this probe took
    return False


def describe(info: dict | None) -> str:
    """One line for the watchdog, explain and the worker about a held marker."""
    if info is None:
        return ""
    if info.get("unreadable"):
        return f"a migration marker {marker_path()} exists but cannot be read"
    since = info.get("started")
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since)) if isinstance(
        since, (int, float)) else "an unknown time"
    if _running():
        return f"`migrate` is running (pid {info.get('pid')}, since {when})"
    return (f"a `migrate` started {when} did not finish (pid {info.get('pid')} is gone): its "
            "records may be half moved — run `migrate` again to finish it")


@contextlib.contextmanager
def hold():
    """Hold the marker for one migration; yields what an interrupted one left, if anything."""
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationBusy(describe(migrating() or {})) from None
        left = migrating()
        state_mod._atomic_write(path, {"pid": os.getpid(), "started": time.time()})
        try:
            yield left
        finally:
            path.unlink(missing_ok=True)
    finally:
        os.close(fd)


def _hermes_config() -> dict:
    """Hermes's own config, read-only (in-process: the plugin runs inside Hermes)."""
    from hermes_cli.config import load_config_readonly  # noqa: PLC0415 - Hermes-only import
    return load_config_readonly() or {}


def old_settings(read=_hermes_config) -> dict:
    """The old plugin's settings-form values, limited to keys this plugin still has."""
    cfg = read() or {}
    plugins = cfg.get("plugins") if isinstance(cfg, dict) else None
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    entry = entries.get(OLD_PLUGIN) if isinstance(entries, dict) else None
    if not isinstance(entry, dict):
        return {}
    out: dict = {}
    for subtree in ("config", "settings"):        # Hermes's legacy subtree, then the current one
        values = entry.get(subtree)
        if isinstance(values, dict):
            out.update({key: value for key, value in values.items()
                        if key in config.SETTINGS_SCHEMA and value not in (None, "")})
    return out


def settings_step(ctx, *, dry_run: bool, read=_hermes_config) -> list[str]:
    if ctx is None:
        return ["settings: no Hermes plugin context — run this through `hermes`; skipped"]
    if getattr(ctx, "plugin_id", OLD_PLUGIN) == OLD_PLUGIN:
        return [f"settings: this is still the {OLD_PLUGIN} plugin — nothing to copy"]
    try:
        old = old_settings(read)
    except Exception as exc:  # noqa: BLE001 - an unreadable config is reported, not guessed
        return [f"settings: could not read the {OLD_PLUGIN} settings ({type(exc).__name__}: "
                f"{exc}) — nothing copied"]
    if not old:
        return [f"settings: {OLD_PLUGIN} has no settings to copy"]
    copy = {key: value for key, value in old.items() if ctx.get_config(key, None) in (None, "")}
    lines = []
    for key in sorted(copy):
        if not dry_run:
            ctx.set_config(key, copy[key])
        lines.append(f"settings: {'would copy' if dry_run else 'copied'} {key}")
    for key in sorted(set(old) - set(copy)):
        lines.append(f"settings: {key} already set here — kept")
    return lines


# -- repository rename ----------------------------------------------------------------------

def renamed_to(loop: dict) -> tuple[str | None, str]:
    """``(new name, "")`` when GitHub reports the loop's repository under another name,
    ``(None, "")`` when it has not moved, ``(None, why)`` when that cannot be told.

    The new name is lowercased, as ``config.normalize`` does to every loop's ``repo``: GitHub's
    ``full_name`` keeps the owner's casing, and the ledger compares names case-sensitively, so a
    mixed-case name written there would never be read back by the loop."""
    old = loop["repo"]
    first = gh.api(loop, f"/repos/{old}", login=loop.get("read_token"))
    if not isinstance(first, dict) or not isinstance(first.get("full_name"), str) \
            or type(first.get("id")) is not int:
        return None, f"could not read {old} from GitHub"
    new = first["full_name"]
    if new.lower() == old.lower():
        return None, ""
    # The name reached by redirect is only trusted when it is the same repository read by name.
    second = gh.api(loop, f"/repos/{new}", login=loop.get("read_token"))
    if not isinstance(second, dict) or second.get("id") != first["id"] \
            or second.get("full_name") != new:
        return None, f"GitHub answered {old} with {new}, but {new} is not the same repository"
    return new.lower(), ""


def _swap(value, old: str, new: str):
    """``value`` with the repository's name moved: an exact name, a ``name#N`` key, and the
    repository's github.com URLs. Nothing else is touched."""
    if isinstance(value, str):
        if value == old:
            return new
        if value.startswith(old + "#"):
            return new + value[len(old):]
        return value.replace(f"github.com/{old}/", f"github.com/{new}/")
    if isinstance(value, list):
        return [_swap(item, old, new) for item in value]
    if isinstance(value, dict):
        return {_swap(key, old, new): _swap(item, old, new) for key, item in value.items()}
    return value


def _ledger_tables(con) -> list[str]:
    names = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return [name for name in names
            if any(col[1] == "repo" for col in con.execute(f'PRAGMA table_info("{name}")'))]


def busy_runs(db: pathlib.Path, repo: str) -> int:
    """Runs for ``repo`` that are claimed, launching, running or uncertain: none may be renamed
    under."""
    from .run_supervisor import ACTIVE  # noqa: PLC0415 - heavy module, only when migrating
    if not db.exists():
        return 0
    con = sqlite3.connect(db, timeout=30)
    try:
        return con.execute(f"SELECT COUNT(*) FROM runs WHERE repo=? AND state IN "
                           f"({','.join('?' * len(ACTIVE))})", (repo, *ACTIVE)).fetchone()[0]
    finally:
        con.close()


def _move_ledger(db: pathlib.Path, old: str, new: str, *, dry_run: bool) -> tuple[int, str]:
    """Rows moved (or that would be); a refusal reason instead while a run is in flight."""
    from .run_supervisor import ACTIVE  # noqa: PLC0415 - heavy module, only when migrating
    if not db.exists():
        return 0, ""
    con = sqlite3.connect(db, timeout=30, isolation_level=None)
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            busy = con.execute(f"SELECT COUNT(*) FROM runs WHERE repo=? AND state IN "
                               f"({','.join('?' * len(ACTIVE))})", (old, *ACTIVE)).fetchone()[0]
            if busy:
                con.execute("ROLLBACK")
                return 0, (f"{busy} run(s) for {old} are in flight or uncertain — wait for them "
                           "(or reconcile them), then run migrate again")
            moved = 0
            for table in _ledger_tables(con):
                moved += con.execute(f'SELECT COUNT(*) FROM "{table}" WHERE repo=?',
                                     (old,)).fetchone()[0]
                if not dry_run:
                    con.execute(f'UPDATE "{table}" SET repo=? WHERE repo=?', (new, old))
            con.execute("ROLLBACK" if dry_run else "COMMIT")
            return moved, ""
        except BaseException:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def _move_state(loop: dict, old: str, new: str, *, dry_run: bool) -> int:
    """State files rewritten (or that would be), under both of the loop's state locks."""
    st = state_mod.state_for(loop)
    if not st.dir.is_dir():
        return 0
    changed = 0
    with st.locked(), st._breach_lock():
        for path in sorted(st.dir.rglob("*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue                       # unreadable state is the loop's own problem
            moved = _swap(data, old, new)
            if moved != data:
                changed += 1
                if not dry_run:
                    state_mod._atomic_write(path, moved)
    return changed


def repo_step(loops: list[dict], write_loop, *, dry_run: bool, ledger: pathlib.Path) -> list[str]:
    lines = []
    for loop in loops:
        new, why = renamed_to(loop)
        old = loop["repo"]
        if why:
            lines.append(f"repo: {loop['id']}: {why} — left as {old}")
            continue
        if new is None:
            lines.append(f"repo: {loop['id']}: {old} has not been renamed")
            continue
        rows, refused = _move_ledger(ledger, old, new, dry_run=dry_run)
        if refused:
            lines.append(f"repo: {loop['id']}: REFUSED — {refused}")
            continue
        files = _move_state(loop, old, new, dry_run=dry_run)
        if not dry_run:
            write_loop(loop, new)
        verb = "would move" if dry_run else "moved"
        lines.append(f"repo: {loop['id']}: {verb} {old} → {new} ({rows} ledger row(s), "
                     f"{files} state file(s), the loop file)")
    return lines


# -- loop id rename (opt-in) ----------------------------------------------------------------
# ``--rename-loop OLD=NEW`` gives a loop a new id. A loop's id names its file, its default state
# directory, its routes (``<id>-review`` and so on) and through them the URLs its GitHub hooks
# post to. The orchestration (routes, hooks, pings) is ``cli._rename_loop``; the pieces that only
# move host files are here.

LOOP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def route_renames(loop: dict, old_id: str, new_id: str) -> dict[str, str]:
    """``{old route name: new}`` for the routes ``loop`` names under either id's convention
    (``<id>-…``). A route the operator named otherwise keeps its name.

    ``loop`` may be the old loop or the renamed one, so a re-run after a crash that left only
    the new loop file still knows which old routes to clear."""
    from .route_intent import routes_of
    out: dict[str, str] = {}
    for name in routes_of(loop).values():
        for this, other in ((old_id, new_id), (new_id, old_id)):
            if name.startswith(f"{this}-"):
                old, new = ((name, other + name[len(this):]) if this == old_id
                            else (other + name[len(this):], name))
                out[old] = new
                break
    return out


def default_state_dir(loop_id: str) -> pathlib.Path:
    return config.default_state_dir(loop_id)


def renamed_loop(loop: dict, new_id: str, renames: dict[str, str]) -> dict:
    """``loop`` under ``new_id``: its routes renamed, and its state directory too when it is the
    default one (a directory the operator chose stays where it is)."""
    new = json.loads(json.dumps(loop))
    new["id"] = new_id
    for seat in ("reviewer", "fixer"):
        entry = (new.get("seats") or {}).get(seat)
        if isinstance(entry, dict) and entry.get("route") in renames:
            entry["route"] = renames[entry["route"]]
    for block in ("adjudicator", "triage"):
        entry = new.get(block)
        if isinstance(entry, dict) and entry.get("route") in renames:
            entry["route"] = renames[entry["route"]]
    observer = new.get("observer")
    if isinstance(observer, dict):
        for key in ("route", "urgent_route"):
            if observer.get(key) in renames:
                observer[key] = renames[observer[key]]
    if config.is_default_state_dir(loop.get("state_dir") or "", loop["id"]):
        new["state_dir"] = str(default_state_dir(new_id))
    return new


def move_state_dir(old: dict, new: dict, *, dry_run: bool) -> str:
    """Move the loop's state directory with its id, once; a re-run finds it already moved."""
    source, target = pathlib.Path(old["state_dir"]), pathlib.Path(new["state_dir"])
    if source == target:
        return "state: kept where it is (not the default directory)"
    if target.exists() and not source.exists():
        return f"state: already at {target}"
    if target.exists():
        raise config.ConfigError(f"both {source} and {target} exist — move one aside by hand")
    if not source.exists():
        return f"state: {source} does not exist yet — nothing to move"
    if not dry_run:
        os.rename(source, target)
    return f"state: {'would move' if dry_run else 'moved'} {source} → {target}"


# -- host files (#425 stage 4) ---------------------------------------------------------------
# The files this plugin keeps under $HERMES_HOME carried the old name (``config.HOST_FILES``).
# Until they move, every reader uses them where they are (``config.host_path``); this step moves
# them, with the install paused and no run in flight anywhere, so nothing holds one open.

# Moved in this order: the run ledger first (it is the record a half-done move must not lose),
# the loop files' own directory last (it is what names everything else).
HOST_MOVES = ("ledger", "state_root", "gate_failures", "pacing", "seat_locks", "runtime",
              "config_dir")


def _old_new(key: str) -> tuple[pathlib.Path, pathlib.Path]:
    new, old = config.HOST_FILES[key]
    return config.home() / old, config.home() / new


def _quiet_ledger(path: pathlib.Path) -> None:
    """Fold the ledger's WAL into the file, so the move carries everything in one file."""
    con = sqlite3.connect(path, timeout=30)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


def files_step(write_loop, *, dry_run: bool) -> list[str]:
    """Move each host file from its old name to its new one; rewrite the loops' state paths."""
    lines: list[str] = []
    ledger = config.host_path("ledger")
    busy = 0
    if ledger.exists():
        from .run_supervisor import ACTIVE  # noqa: PLC0415 - heavy module, only when migrating
        con = sqlite3.connect(ledger, timeout=30)
        try:
            busy = con.execute(f"SELECT COUNT(*) FROM runs WHERE state IN "
                               f"({','.join('?' * len(ACTIVE))})", ACTIVE).fetchone()[0]
        except sqlite3.Error:
            busy = 0                        # no runs table yet: nothing can be in flight
        finally:
            con.close()
    if busy:
        return [f"files: REFUSED — {busy} run(s) are in flight or uncertain; wait for them (or "
                "reconcile them), then run migrate again"]
    old_root, new_root = _old_new("state_root")
    for key in HOST_MOVES:
        if key == "config_dir" and envnames.get("CONFIG_DIR"):
            lines.append("files: the loop files' directory is set by DIAKTOROS_CONFIG_DIR — kept")
            continue
        old, new = _old_new(key)
        if not os.path.lexists(old):
            continue
        if os.path.lexists(new):
            lines.append(f"files: REFUSED — both {old} and {new} exist; move one aside by hand")
            continue
        if not dry_run:
            if key == "ledger":
                _quiet_ledger(old)
            new.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old, new)
            if key == "ledger":
                for suffix in ("-wal", "-shm"):
                    side = old.with_name(old.name + suffix)
                    if side.exists():
                        os.rename(side, new.with_name(new.name + suffix))
        lines.append(f"files: {'would move' if dry_run else 'moved'} {old} → {new}")
    if dry_run:
        return lines or ["files: every host file already has its new name"]
    for loop in config.all_loops():
        state = pathlib.Path(str(loop.get("state_dir") or ""))
        try:
            rest = state.relative_to(old_root)
        except ValueError:
            continue
        write_loop({**loop, "state_dir": str(new_root / rest)})
        lines.append(f"files: {loop['id']}: state_dir now {new_root / rest}")
    return lines or ["files: every host file already has its new name"]


# -- shims and the closing check ------------------------------------------------------------

def shim_step(loops: list[dict], write_watchdog_shim, watchdog_shim: pathlib.Path, wanted: str,
              *, dry_run: bool) -> list[str]:
    lines = []
    for loop in loops:
        try:
            written = gate_shims.install(loop, dry_run=dry_run, report=False) or []
        except (gate_shims.ShimError, OSError) as exc:
            lines.append(f"shims: {loop['id']}: NOT rewritten — {exc}")
            continue
        lines += [f"shims: {loop['id']}: {line}" for line in written]
    if watchdog_shim.is_file() and not watchdog_shim.is_symlink():
        if watchdog_shim.read_text() != wanted:
            if not dry_run:
                write_watchdog_shim()
            lines.append(f"shims: watchdog shim {'would be ' if dry_run else ''}pointed at "
                         "this plugin")
    if not lines:
        lines.append("shims: already point at this plugin")
    return lines


def doctor_step(loops: list[dict]) -> list[str]:
    from . import doctor  # noqa: PLC0415 - heavy module
    lines = []
    for loop in loops:
        for check in doctor.check_loop(loop):
            if check.status not in (doctor.VERIFIED, doctor.SKIPPED):
                lines.append(f"doctor: {loop['id']}: {check.name} {check.status} — {check.detail}"
                             + (f" (fix: {check.fix})" if check.fix else ""))
    return lines or ["doctor: every check verified"]


def old_plugin_note(plugins_dir: pathlib.Path) -> list[str]:
    """Name the old plugin for removal only when that folder still holds it: an in-place update
    keeps the folder's old name, and then it is this plugin, not a leftover."""
    try:
        manifest = (plugins_dir / OLD_PLUGIN / "plugin.yaml").read_text()
    except OSError:
        return []
    found = re.search(r"^name:\s*['\"]?([A-Za-z0-9_.-]+)", manifest, re.M)
    if not found or found.group(1) != OLD_PLUGIN:
        return []
    return [f"next: once you are happy, remove the old plugin: "
            f"`hermes plugins remove {OLD_PLUGIN}` (its settings stay in config.yaml)"]
