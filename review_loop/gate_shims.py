"""Gate shims: put each route's script where the gateway actually looks for it (issue #105).

A route names its script bare (``gate_reviewer.py``), and Hermes's webhook gateway resolves that
name in exactly one place — ``gateway/platforms/webhook_filters.py::_resolve_script_path``:
``get_hermes_home()/scripts/<name>``, resolved, and refused unless the result is still *inside*
that directory. A symlink into the plugin therefore resolves outside it and is refused too; only a
real file works.

``get_hermes_home()`` is the **serving profile's** home, not the plugin's: the gateway runs a
route's script inside ``_profile_scope(profile)``, which is ``get_profile_dir(profile)`` —
``<root>`` for ``default`` and ``<root>/profiles/<name>`` otherwise. (A ``/p/<name>/`` route only
reaches a scope at all through a multiplexed gateway at the root, the one that reads the root's
``webhook_subscriptions.json`` this plugin writes; a bare ``/webhooks/<name>`` route runs in the
gateway's own home, the root. Both are ``config.profile_dir(route.profile)``.)

So for every loop route, the plugin writes a small shim named after the gate into that profile's
``scripts/``. The shim ``runpy``s the plugin's own script by absolute path, with the script's own
``__file__``, ``sys.path[0]``, ``sys.argv[0]`` and working directory, in the same process — stdin,
stdout, stderr and the exit status are the plugin script's own. The plugin stays the one copy of
the code: an upgrade in place needs no reinstall, and a shim pinned to a plugin path that moved is
"stale" and rewritten by ``apply``.

A file of the same name that this plugin did not write is never overwritten or removed.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

from . import config, route_intent, routes

GATE_SCRIPT = route_intent.GATE_SCRIPT
MARKER = "# hermes-review-loop gate shim"
MODE = 0o755          # the gateway runs [sys.executable, path], so read suffices; x lets a human run it

SHIM = '''#!/usr/bin/env python3
{marker} — written by `hermes review-loop init/apply`; do not edit.
"""The webhook gateway only runs a route's script from this profile's scripts/ directory, and
refuses one that resolves outside it (so a symlink will not do). This runs the plugin's own gate
in this process, exactly as if the gateway had run it by its real path."""
import os
import runpy
import sys

TARGET = {target!r}
if not os.path.isfile(TARGET):
    sys.stderr.write("hermes-review-loop: gate script missing: " + TARGET
                     + " — reinstall the plugin, then `hermes review-loop apply`\\n")
    sys.exit(1)
if sys.path and os.path.realpath(sys.path[0] or os.curdir) == os.path.dirname(os.path.realpath(__file__)):
    sys.path[0] = os.path.dirname(TARGET)
sys.argv[0] = TARGET
os.chdir(os.path.dirname(TARGET))
runpy.run_path(TARGET, run_name="__main__")
'''


class ShimError(config.ConfigError):
    """A shim cannot be put where the gateway looks (foreign file, missing profile home)."""


def plugin_script(script: str) -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[1] / "scripts" / script


def render(script: str) -> str:
    return SHIM.format(marker=MARKER, target=str(plugin_script(script)))


def home_for(profile: str | None) -> pathlib.Path:
    """The home the gateway scopes a route of this profile to (see the module docstring)."""
    return config.profile_dir(str(profile or "default"))


def resolve(home: pathlib.Path, script_value) -> tuple[pathlib.Path | None, str | None]:
    """Hermes's ``_resolve_script_path`` (pinned f84db42a), with ``get_hermes_home()`` = ``home``.

    Kept line-for-line with the gateway so ``doctor`` fails exactly where the gateway would drop
    the event; ``tests/test_gate_shims.py`` runs both against the same layout.
    """
    if not isinstance(script_value, str) or not script_value.strip():
        return None, "script path is empty"
    scripts_root = (pathlib.Path(home) / "scripts").resolve()
    raw_text = os.path.expandvars(script_value.strip())
    if raw_text == "~/.hermes" or raw_text.startswith("~/.hermes/"):
        candidate = (pathlib.Path(home) / raw_text[len("~/.hermes/"):]).resolve()
    else:
        raw = pathlib.Path(raw_text).expanduser()
        candidate = raw.resolve() if raw.is_absolute() else (scripts_root / raw).resolve()
    if not candidate.is_relative_to(scripts_root):
        return None, f"script path resolves outside {scripts_root}"
    if not candidate.exists():
        return None, f"script not found: {candidate}"
    return (candidate, None) if candidate.is_file() else (None, f"script path is not a file: {candidate}")


def _ours(text: str | None) -> bool:
    return text is not None and text.startswith("#!/usr/bin/env python3\n" + MARKER)


def _read(path: pathlib.Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def state(home: pathlib.Path, script: str) -> tuple[str, pathlib.Path, str]:
    """(status, path, detail) for one gate in one home: ok | missing | stale | foreign | nohome."""
    path = pathlib.Path(home) / "scripts" / script
    if not pathlib.Path(home).is_dir():
        return "nohome", path, f"profile home {home} does not exist"
    if not os.path.lexists(path):
        return "missing", path, f"no {path}"
    if path.is_symlink() or not path.is_file():
        return "foreign", path, f"{path} is not a regular file this plugin wrote"
    text = _read(path)
    if not _ours(text):
        return "foreign", path, f"{path} exists and was not written by hermes-review-loop"
    if text != render(script):
        return "stale", path, f"{path} is an older review-loop shim (not pinned to {plugin_script(script)})"
    return "ok", path, f"{path} → {plugin_script(script)}"


def config_profile(loop: dict, role: str) -> str:
    if role == "observer":
        return str((loop.get("observer") or {}).get("profile") or "default")
    return config.seat_profile(loop, role)


def wanted(loop: dict) -> set[tuple[str, str]]:
    """(profile, script) for each route this loop's config installs."""
    out = set()
    for role in route_intent.routes_of(loop):
        profile = config_profile(loop, role)
        if profile:                     # a seat with no profile: see ``divergence``
            out.add((profile, GATE_SCRIPT[role]))
    return out


def _side(profile: str, script: str) -> str:
    return f"{profile or '(no profile)'}/{script}"


def recreate_fix(loop: dict) -> str:
    """The command that writes a loop's missing routes when no intent record can restore them.
    ``init`` refuses an existing loop, so it is never the answer here."""
    return (f"`hermes review-loop apply --loop {loop.get('id', '?')} --recreate-routes` (writes "
            "it from the loop config with a new secret and re-keys the repo hook that points at it)")


def divergence(loop: dict, *, include_missing: bool = True) -> dict:
    """route name → (detail, fix, status) for each loop route whose registry entry is not what the
    loop config would install: another profile or gate, no profile in the config, or no route.

    The shim follows the registry (that is what the gateway runs), so a disagreement here is never
    fixed by writing shims alone; each one names both sides and the command that reconciles them.
    """
    registry = routes.all_routes()
    try:
        intent = route_intent.load(loop) or {}
    except route_intent.IntentError:
        intent = {}
    lid = loop.get("id", "?")
    out = {}
    for role, name in sorted(route_intent.routes_of(loop).items()):
        want = (config_profile(loop, role), GATE_SCRIPT[role])
        entry = registry.get(name) if isinstance(registry, dict) else None
        recorded = intent.get(name) if isinstance(intent.get(name), dict) else None
        restorable = recorded is not None and (
            str(recorded.get("profile") or "default"), recorded.get("script")) == want
        repair = f"`hermes review-loop doctor --loop {lid} --repair` (restores the route the plugin wrote)"
        if not isinstance(entry, dict):
            if include_missing:
                out[name] = (f"loop config says {_side(*want)}, but the registry holds no route "
                             f"{name!r} — the gateway 404s this seat",
                             repair if restorable else recreate_fix(loop), "absent")
            continue
        profile = entry.get("profile", "default")
        have = (profile if isinstance(profile, str) and profile.strip() else "default",
                entry.get("script"))
        if have == want:
            continue
        detail = f"registry runs {_side(*have)}, loop config says {_side(*want)}"
        if not want[0]:
            fix = (f"name the {role} seat's profile (its `profile` under `seats.{role}` in the loop config, or "
                   f"the plugin settings' {role} profile), then `hermes review-loop apply --loop "
                   f"{lid}`")
        elif restorable:
            fix = f"{repair} — or, if the registry is right, change the config and apply"
        else:
            fix = f"`hermes review-loop apply --loop {lid}` (rebinds the route to the config)"
        out[name] = (detail, fix, "mismatch")
    return out


def live(loop: dict) -> set[tuple[str, str]]:
    """(profile, script) for this loop's routes as the registry holds them now — what the gateway
    will actually run. Only entries still on one of this plugin's gates count."""
    registry = routes.all_routes()
    out = set()
    for name in route_intent.routes_of(loop).values():
        entry = registry.get(name) if isinstance(registry, dict) else None
        if isinstance(entry, dict) and route_intent.owned(entry):
            profile = entry.get("profile", "default")
            out.add((profile if isinstance(profile, str) and profile.strip() else "default",
                     entry["script"]))
    return out


def _plan(pairs) -> list[tuple[str, pathlib.Path, str, str]]:
    """(status, path, script, detail) per distinct target file; raises ShimError on any refusal."""
    plan, seen, refusals = [], set(), []
    for profile, script in sorted(pairs):
        status, path, detail = state(home_for(profile), script)
        if path in seen:
            continue
        seen.add(path)
        if status == "foreign":
            refusals.append(f"{detail} — the gateway runs {script} from there for profile "
                            f"{profile!r}; move it aside, then re-run")
        elif status == "nohome":
            refusals.append(f"{detail} (profile {profile!r}) — create the profile first")
        plan.append((status, path, script, detail))
    if refusals:
        raise ShimError("gate shim refused: " + "; ".join(refusals))
    return plan


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(mode=0o755, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, MODE)
        # Re-check just before publishing: never replace a file someone else put there meanwhile.
        if os.path.lexists(path) and (path.is_symlink() or not _ours(_read(path))):
            raise ShimError(f"{path} appeared and was not written by hermes-review-loop; left alone")
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def install(loop: dict, *, dry_run: bool = False, pairs=None, report: bool = True) -> list[str]:
    """Write every missing or stale shim this loop needs; idempotent. Raises ShimError/OSError.

    Refuses (before writing anything) only over what the config installs. What the registry runs
    now is written too where it can be, and every route where the two disagree is reported
    (``divergence``) — never skipped in silence. ``report=False`` leaves that to the caller.
    """
    lines = []
    plan = _plan(wanted(loop) if pairs is None else pairs)
    if pairs is None:
        # What the registry runs today, where the config would not install it: written when it
        # can be (the gateway runs it until the route is reconciled), never a reason to refuse —
        # `apply` is about to rebind exactly these routes away.
        planned = {path for _s, path, _sc, _d in plan}
        for profile, script in sorted(live(loop) - wanted(loop)):
            status, path, detail = state(home_for(profile), script)
            if path in planned:
                continue
            if status in ("foreign", "nohome"):
                if report:
                    lines.append(f"⚠️ gate shim not written for profile {profile!r}: {detail}")
                continue
            planned.add(path)
            plan.append((status, path, script, detail))
    for status, path, script, _detail in plan:
        if status == "ok":
            continue
        verb = "would write" if dry_run else ("rewrote" if status == "stale" else "wrote")
        if not dry_run:
            _write(path, render(script))
        lines.append(f"gate shim {verb}: {path}")
    if pairs is None and report:
        lines += [f"⚠️ route {name}: {detail} — fix: {fix}"
                  for name, (detail, fix, _status) in divergence(loop).items()]
    return lines


def heal(loop: dict) -> list[str]:
    """The watchdog's self-heal for shims, for the routes the registry holds: alert lines only.

    Each refusal says what the gateway does meanwhile, in doctor's words: with a foreign file of
    the gate's name it *runs that instead of the gate*; with no profile home it runs nothing and
    drops the route's events."""
    label = f"[{loop.get('id', '?')}] {loop.get('repo', '')}".rstrip()
    lid = loop.get("id", "?")
    alerts, pairs = [], set()
    for profile, script in sorted(live(loop)):
        status, path, detail = state(home_for(profile), script)
        if status == "foreign":
            alerts.append(f"⚠️ Review loop {label} — gate shim NOT restored: {detail}; the gateway "
                          f"runs that instead of the gate for profile {profile!r}. Move it aside, "
                          f"then `hermes review-loop apply --loop {lid}`.")
        elif status == "nohome":
            alerts.append(f"⚠️ Review loop {label} — gate shim NOT restored: {detail}, so the "
                          f"gateway drops every event on the routes it serves as {profile!r}. "
                          f"`hermes review-loop doctor --loop {lid}` names the fix.")
        else:
            pairs.add((profile, script))
    try:
        written = install(loop, pairs=pairs)
    except (OSError, config.ConfigError) as exc:
        return alerts + [f"⚠️ Review loop {label} — gate shims NOT restored ({exc}); the gateway "
                         "drops this loop's events where a shim is missing"]
    if written:
        alerts += [f"🔧 Review loop {label} — restored {len(written)} gate shim(s) the gateway "
                   "needs:", *(f"  {line}" for line in written)]
    return alerts


def remove(loop: dict, keep_loops: list[dict]) -> list[str]:
    """Remove this loop's shims that no loop in ``keep_loops`` still needs. Ours only."""
    keep = {(home_for(p) / "scripts" / s) for other in keep_loops for p, s in wanted(other)}
    lines = []
    for profile, script in sorted(wanted(loop)):
        status, path, _ = state(home_for(profile), script)
        if path in keep or status not in ("ok", "stale"):
            continue
        path.unlink(missing_ok=True)
        lines.append(f"gate shim removed: {path}")
    return lines


def live_checks(loop: dict) -> list[tuple[str, str, str, str]]:
    """(name, status, detail, fix) per installed loop route: resolve its script as the gateway does.

    status is ``ok`` | ``absent`` | ``mismatch``. A route whose registry entry and loop config
    disagree (or that the registry lacks) is reported as that, with both sides named.
    """
    registry = routes.all_routes()
    fix = f"`hermes review-loop apply --loop {loop.get('id', '?')}` (writes the gate shims)"
    diverged = divergence(loop)
    out = []
    for role, name in sorted(route_intent.routes_of(loop).items()):
        if name in diverged:
            detail, remedy, status = diverged[name]
            out.append((f"gateway-script:{name}", status, detail, remedy))
            continue
        entry = registry.get(name) if isinstance(registry, dict) else None
        if not isinstance(entry, dict):
            continue
        profile = entry.get("profile", "default")
        script = entry.get("script")
        home = home_for(profile if isinstance(profile, str) and profile.strip() else "default")
        label = f"gateway-script:{name}"
        found, error = resolve(home, script)
        if error or found is None:
            out.append((label, "absent", f"the gateway would drop every event: {error} "
                        f"(profile {profile!r})", fix))
            continue
        if script not in route_intent.PLUGIN_SCRIPTS or found.parent.name != "scripts":
            out.append((label, "mismatch", f"{script!r} resolves to {found}, not a review-loop gate",
                        fix))
            continue
        status, _path, detail = state(home, found.name)
        if status == "ok":
            out.append((label, "ok", f"{script} → {detail}", ""))
        elif status == "stale":
            out.append((label, "mismatch", detail, fix))
        else:
            out.append((label, "mismatch", f"{detail}; the gateway runs that instead of the gate",
                        f"move {found} aside, then {fix}"))
    return out
