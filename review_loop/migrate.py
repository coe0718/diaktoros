"""One-time move from the ``hermes-review-loop`` plugin to its new name (#425).

Hermes knows a plugin by its manifest ``name``: the install folder, ``plugins.enabled`` and
``plugins.entries.<name>.settings`` are all keyed by it. A renamed plugin is therefore a new
plugin to Hermes: it starts with a blank settings form, and the gate and watchdog shims still run
the old folder's scripts. A renamed GitHub repository is the same story for the loop: the loop
file, the run ledger and the loop's state all name the repository, and a webhook from the
renamed repository matches no loop at all.

``migrate`` closes those gaps, in an order that a crash or a re-run cannot hurt:

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

import json
import pathlib
import sqlite3

from . import config, gh, gate_shims, state as state_mod

OLD_PLUGIN = "hermes-review-loop"


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
    ``(None, "")`` when it has not moved, ``(None, why)`` when that cannot be told."""
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
    return new, ""


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
    if (plugins_dir / OLD_PLUGIN).exists():
        return [f"next: once you are happy, remove the old plugin: "
                f"`hermes plugins remove {OLD_PLUGIN}` (its settings stay in config.yaml)"]
    return []
