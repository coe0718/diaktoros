"""Whether a ``hermes dk restore`` archive is safe to restore on this install (#496, #587, #783,
#917, #920): ``unsafe`` and the route and state-directory checks it is built from.

An archive is input from outside. ``backup`` reads and writes it; this module only judges what
it would write, and never writes anything. Split out of ``backup`` (#1049's PR took that file
over the size ratchet).
"""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import tarfile

from . import config, gate_shims, route_intent, routes


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
    # backup.put_files() also creates every absolute state directory, with or without member files
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
