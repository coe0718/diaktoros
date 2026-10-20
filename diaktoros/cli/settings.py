"""Changing a loop (#676): ``set``, ``apply``, ``show``, ``settings``, ``triage`` and
``fixer-push``, with the route and hook reconciliation a change needs.

``cli`` re-exports every command here. The install plumbing these share with ``init`` and
``uninstall``, the settings form's values and the branch-push refusal are ``cli_common``'s.
"""
from __future__ import annotations

import json
import pathlib
import shlex

from .. import (
    config,
    doctor,
    gate_shims,
    gh,
    observer,
    prompts,
    route_intent,
    routes,
    state as state_mod,
)
from . import common as cli_common
from ..config import credentials
from ..config import settings_form
from .common import _ROUTE_PROMPT, BRANCH_PUSH_REFUSAL, GATE_SCRIPT


def _role_summary(loop: dict, role: str) -> str:
    profile = config.seat_profile(loop, role) or "(no profile)"
    if role == "adjudicator":
        if not (loop.get("adjudicator") or {}).get("route"):
            return "adjudicator (none)"
        return f"adjudicator {profile}"
    return f"{role} {config.seat_login(loop, role) or '(no login)'} ({profile})"


def _seat_diffs(was: dict, now: dict) -> tuple[list[tuple[str, object, object]], set[str]]:
    """The seat identities the settings move, and which roles that touches.

    A role is *touched* when its profile, its login, or the login its review route serves moves:
    all three are "who does what", and each one owes the same validation, the same in-flight check
    and the same route rebind.
    """
    diffs: list[tuple[str, object, object]] = []
    touched: set[str] = set()
    for role in ("reviewer", "fixer"):
        for key in ("profile", "login"):
            before = str((was["seats"].get(role) or {}).get(key) or "")
            after = str((now["seats"].get(role) or {}).get(key) or "")
            if before != after:
                diffs.append((f"{role} {key}", before or "(none)", after or "(none)"))
                touched.add(role)
    review_seat_before = str(was.get("reviewer_seat") or "")
    review_seat_after = str(now.get("reviewer_seat") or "")
    if review_seat_before != review_seat_after and "reviewer" not in touched:
        # Only worth its own line when nothing else already explains it: the two move together
        # through the settings, and a duplicate line reads like two changes.
        diffs.append(("reviewer_seat", review_seat_before or "(none)", review_seat_after or "(none)"))
        touched.add("reviewer")
    adj_before = str((was.get("adjudicator") or {}).get("profile") or "")
    adj_after = str((now.get("adjudicator") or {}).get("profile") or "")
    if adj_before != adj_after:
        diffs.append(("adjudicator profile", adj_before or "(none)", adj_after or "(none)"))
        touched.add("adjudicator")
    # A seat's token file is part of who it is: a new path for a login a seat acts as is an
    # identity change, owed the same in-flight check. Paths only — the file is never opened here.
    for role in ("reviewer", "fixer"):
        login = config.seat_login(now, role)
        before, after = cli_common._token_ref(was, login), cli_common._token_ref(now, login)
        if before != after:
            diffs.append((f"{role} token file ({login})", before or "(none)", after or "(none)"))
            touched.add(role)
    # The adjudicator's comment identity has no run of its own to strand: it is reported, and
    # validated, but it does not rebind a route.
    login_before, login_after = config.adjudicator_login(was), config.adjudicator_login(now)
    if login_before != login_after:
        diffs.append(("adjudicator login", login_before or "(none)", login_after or "(none)"))
    if login_after:
        before, after = cli_common._token_ref(was, login_after), cli_common._token_ref(now, login_after)
        if before != after:
            diffs.append((f"adjudicator token file ({login_after})", before or "(none)",
                          after or "(none)"))
    return diffs, touched


def _route_binds(loop: dict, touched: set[str]) -> dict:
    """role → (route name, installed profile, target profile) for routes a seat change rebinds."""
    binds: dict = {}
    for role, name in cli_common._routes_of(loop).items():
        if role not in touched:
            continue
        entry = routes.route(name)
        if not entry:
            continue
        current = routes.route_profile(entry) or ""   # "" = blank: the gateway refuses it
        target = config.seat_profile(loop, role)
        if current != target:
            binds[role] = (name, current, target)
    return binds


def _drifted_routes(loop: dict) -> dict:
    """role → (route name, fields) for this loop's own routes whose registry entry differs from
    what the plugin writes (``gate_shims.contract_drift``) in a way apply can repair.

    Only a route provably ours (its role's gate *and* prompt) counts; rewriting it from the config
    keeps its secret. That covers the gateway's 403 on ``enabled: false``, an observer that lost
    ``deliver_only`` (which would wake an agent) or its destination, and a seat off its event.
    """
    out: dict = {}
    for role, name in cli_common._routes_of(loop).items():
        entry = routes.route(name)
        if (isinstance(entry, dict) and entry.get("script") == GATE_SCRIPT[role]
                and entry.get("prompt") == _ROUTE_PROMPT[role]):
            fields = gate_shims.contract_drift(loop, role, entry)
            if fields:
                out[role] = (name, fields)
    return out


def _stale_scripts(loop: dict) -> dict:
    """role → (route name, installed script) for this loop's routes still on an older gate.

    Only a script this plugin itself once installed for that role counts, and only with the
    role's own prompt: anything else is not provably ours and ``_verify_routes`` refuses it.
    """
    stale: dict = {}
    for role, name in cli_common._routes_of(loop).items():
        entry = routes.route(name)
        if not isinstance(entry, dict):
            continue
        script = entry.get("script")
        if (script in config.LEGACY_GATE_SCRIPTS.get(role, ())
                and entry.get("prompt") == _ROUTE_PROMPT[role]):
            stale[role] = (name, script)
    return stale


def _keep_one(candidates: list[dict], dest: str) -> tuple[dict, list[dict]]:
    """Of several repo hooks for one route, the one that stays: an ACTIVE hook first (a route must
    never end up with fewer armed hooks than it had), then one already at ``dest``, then the
    oldest (lowest id). The rest are redundant — named for deletion, never moved or deleted."""
    ordered = sorted(candidates, key=lambda hook: (hook.get("active") is not True,
                                                  not routes.serves_route_url(hook["config"]["url"], dest),
                                                  hook["id"]))
    return ordered[0], ordered[1:]


def _hook_moves(before: dict, after: dict, binds: dict, token_login: str | None = None, *,
                check_only: bool = False) -> tuple[list, list]:
    """Preflight exact hook URLs against configured and installed owned route profiles."""
    names = cli_common._routes_of(after)
    expected: dict[str, tuple[str, str]] = {}
    targets: dict[str, str] = {}
    unchanged: dict[str, str] = {}
    for role in config.hook_roles(after):
        name = names.get(role)
        if not name or name != cli_common._routes_of(before).get(role):
            continue
        new = routes.url_for_profile(name, config.seat_profile(after, role), after.get("host"))
        old = routes.url_for_profile(name, config.seat_profile(before, role), before.get("host"))
        if not old or not new:
            raise config.ConfigError(f"cannot resolve {role} hook URL; no changes made")
        if old != new:
            expected[old] = (role, new)
        if role in binds:
            # The registry can drift while loop config and form still agree. Its owned route's
            # installed profile is another known old URL, not an arbitrary hook destination.
            installed = routes.url_for_profile(name, binds[role][1], before.get("host"))
            if not installed:
                raise config.ConfigError(f"cannot resolve installed {role} hook URL; no changes made")
            if installed != new:
                expected[installed] = (role, new)
            targets[role] = new
        elif old != new:
            targets[role] = new
        else:
            unchanged[role] = new
    route_names = {name: role for role, name in names.items() if role in config.hook_roles(after)}
    if not targets and not (check_only and unchanged):
        # Nothing moves: a settings push reads no hooks. Only plain `apply` (check_only) reads
        # them anyway, to name duplicates and repoint a slashed hook nothing else would report.
        return [], []
    if targets:
        hooks = cli_common._hook_listing(before, token_login)  # Never repair a route if this listing cannot be trusted.
    else:
        # Nothing moves, so an unreadable listing blocks nothing — but a readable one is still
        # checked for two hooks on one route URL, which nothing else would ever report.
        try:
            hooks = cli_common._hook_listing(before, token_login)
        except config.ConfigError as exc:
            print(f"  (repo hooks not checked for duplicates: {exc})")
            return [], []
    moves, redundant = [], []
    for name, role in route_names.items():
        mine = [hook for hook in hooks if routes.route_name_of(hook["config"]["url"]) == name]
        if role in targets:
            dest = targets[role]
            for hook in mine:
                old = hook["config"]["url"]
                # A trailing slash on the target or a known old URL is the same route, spelled so
                # the gateway 404s it: repairable by a move, never "unexpected".
                known = routes.same_webhook_url(old, dest) or any(
                    routes.same_webhook_url(old, url) and owner == role
                    for url, (owner, _new) in expected.items())
                if not known:
                    raise config.ConfigError(f"installed {role} hook {hook['id']} "
                                             f"points at unexpected URL {old!r}; no changes made")
        elif role in unchanged:
            dest = unchanged[role]
            # Every hook on this route is left (already right), repaired (a trailing slash on this
            # URL), or refused — never skipped in silence (#55): one posting anywhere else is not
            # something this push can explain.
            for hook in mine:
                if not routes.same_webhook_url(hook["config"]["url"], dest):
                    raise config.ConfigError(
                        f"installed {role} hook {hook['id']} points at unexpected URL "
                        f"{hook['config']['url']!r}; no changes made — `hermes dk doctor "
                        f"--loop {after.get('id')}` names what it is")
        else:
            continue
        if not mine:
            continue
        keep, rest = _keep_one(mine, dest)
        if not routes.serves_route_url(keep["config"]["url"], dest):
            moves.append((keep["id"], keep["config"]["url"], dest, keep["config"].get("insecure_ssl")))
        redundant += [(hook, keep) for hook in rest]
    return moves, redundant


def _hook_origin(loop: dict, drifted: dict) -> dict:
    """The loop as its repo hooks still know it, for ``_hook_moves``' "before" side.

    After ``set --host`` the loop config already names the new origin while the seat routes (and
    the hooks that post to them) still carry the old one; apply rewrites the routes' origin
    (``drifted`` ⊇ "host"), so the hooks' old URLs are the routes' *recorded* origin. Handing
    that to ``_hook_moves`` moves the hooks with the routes through #106's own path — its
    listing, active-first choice, duplicate naming and full-config PATCH — unchanged.
    """
    hosts = {str((routes.route(name) or {}).get("host") or "").removesuffix("/")
             for role, (name, fields) in drifted.items()
             if role in config.HOOK_EVENT and "host" in fields}
    if not hosts:
        return loop
    if len(hosts) > 1:
        raise config.ConfigError("the reviewer and fixer routes record different gateway origins; "
                                 "cannot tell which one their repo hooks post to — no changes made")
    return {**loop, "host": hosts.pop()}


def _ensure_hooks(loop: dict, token_login: str | None, dry_run: bool,
                  outcome: dict | None = None) -> int:
    """``apply --hooks``: make this loop's two repo hooks what its routes need, the way ``init
    --hooks`` would have for a new loop (``init`` refuses an existing one).

    Per seat, hooks are matched by route name and one is kept by #106's ``_keep_one`` (active
    first). A seat with none gets one created, paused until ``arm``. The kept hook is repointed at
    the route's exact URL with ``_patch_hook_url`` (full config: the route's secret, the hook's own
    TLS setting, json), and given the gate's event if it lacks it. Extra hooks are only named. The
    listing is read first and nothing is written if it cannot be trusted; returns an exit code.
    """
    names = cli_common._routes_of(loop)
    try:
        host = config.webhook_host(loop.get("host"), required=True)
        listing = cli_common._hook_listing(loop, token_login)
        plans, missing = [], []
        for seat in config.hook_roles(loop):
            event = config.HOOK_EVENT[seat]
            name = names.get(seat, "")
            url = routes.url_for(name, host) if name else None
            if not url or not (routes.route(name) or {}).get("secret"):
                raise config.ConfigError(f"route {name!r} needs a webhook URL and a secret before "
                                         "its hook can be written")
            mine = [hook for hook in listing if routes.route_name_of(hook["config"]["url"]) == name]
            if not mine:
                missing.append((seat, url))
                continue
            keep, rest = _keep_one(mine, url)
            todo = []
            if (not routes.serves_route_url(keep["config"]["url"], url)
                    or keep["config"].get("content_type") != "json"):
                todo.append("config")
            if event not in (keep.get("events") or []):
                todo.append("event")
            plans.append((seat, event, url, keep, rest, todo))
    except config.ConfigError as exc:
        print(f"refused: repo hooks not reconciled: {exc}")
        print(f"  fix: re-run with --admin-token <login> ({cli_common.hook_write_need(loop, token_login)})")
        return 2
    if outcome is not None:
        outcome["created"] = [seat for seat, _ in missing]
        outcome["kept"] = {seat: keep for seat, _e, _u, keep, _r, _t in plans}
    verb = "would " if dry_run else ""
    for seat, url in missing:
        print(f"  hook for {names[seat]}: {verb}create → {url} (paused until `arm`)")
    for seat, event, url, keep, rest, todo in plans:
        if "config" in todo:
            print(f"  hook {keep['id']}: {verb}repoint → {url} (json, secret and TLS kept)")
        if "event" in todo:
            print(f"  hook {keep['id']}: {verb}add event {event!r}")
        for hook in rest:
            print(f"  {_redundant_hook_line(loop, hook, keep)}")
    redundant = any(rest for *_ignored, rest, _todo in plans)
    if dry_run:
        return 1 if redundant else 0
    login = token_login or loop.get("read_token")
    try:
        if missing:
            for line in cli_common._install_hooks(loop, token_login, seats=[seat for seat, _ in missing]):
                print(f"  {line}")
        for seat, event, url, keep, rest, todo in plans:
            if "config" in todo:
                cli_common._patch_hook_url(loop, keep["id"], url, token_login,
                                insecure_ssl=keep["config"].get("insecure_ssl"))
                print(f"  hook {keep['id']} → {url}")
            if "event" in todo:
                path = f"/repos/{loop['repo']}/hooks/{keep['id']}"
                gh.api(loop, path, method="PATCH", body={"add_events": [event]}, login=login)
                actual = gh.api(loop, path, login=login)
                if not isinstance(actual, dict) or event not in (actual.get("events") or []):
                    raise config.ConfigError(f"hook {keep['id']} event {event!r} not confirmed — "
                                             f"{cli_common.hook_write_need(loop, token_login)}")
                print(f"  hook {keep['id']} now subscribes to {event!r}")
    except config.ConfigError as exc:
        print(f"repo hooks NOT fully reconciled: {exc}")
        print(f"  fix: re-run with --admin-token <login> ({cli_common.hook_write_need(loop, token_login)})")
        return 2
    return 1 if redundant else 0


def _redundant_hook_line(loop: dict, hook: dict, keep: dict) -> str:
    def state(h: dict) -> str:
        return "active" if h["active"] else "paused"
    return (f"⚠️ hook {hook['id']} ({state(hook)}) at {hook['config']['url']} is redundant — hook "
            f"{keep['id']} ({state(keep)}) is the one kept for this route, so this one was left "
            f"where it is rather than duplicated. Delete it: "
            f"`gh api -X DELETE repos/{loop['repo']}/hooks/{hook['id']}`")


def _recreate_routes(loop: dict, missing: dict, *, dry_run: bool,
                     token_login: str | None = None) -> tuple[int, int]:
    """``apply --recreate-routes``: write routes the registry lost when no intent record can
    restore them. The old secret is gone with the route, so each gets a new one and the repo hook
    that points at the route's URL is re-keyed to it — a route whose hook still signs with the old
    secret would reject every delivery. Refuses, writing nothing, if the hooks cannot be read.

    Hooks are matched by route name, as ``_hook_moves`` does, and one is kept by ``_keep_one``
    (active first, then already at the route's URL, then lowest id): it is re-keyed, and moved to
    the route's URL if it was left at the loop's previous one. Any further hook for the route is
    never duplicated onto its URL — it is named with the command that deletes it. Returns
    ``(rc, redundant hooks named)``."""
    roles = set(missing)
    names = ", ".join(sorted(missing.values()))
    try:
        credentials.verify_seats(loop, roles & set(config.SEAT_KEYS))
        cli_common._verify_routes(loop, roles)
        hooks = cli_common._hook_listing(loop, token_login)
    except config.ConfigError as exc:
        print(f"refused: cannot recreate {names}: {exc}")
        if isinstance(exc, cli_common.HookAccessError):
            print(f"  fix: {cli_common._hook_fix(loop, exc, token_login)}")
        return 2, 0
    urls = {name: routes.url_for_profile(name, config.seat_profile(loop, role), loop.get("host"))
            for role, name in missing.items()}
    rekey, redundant = [], []
    for name, url in urls.items():
        mine = [hook for hook in hooks if routes.route_name_of(hook["config"]["url"]) == name]
        if url and mine:
            keep, rest = _keep_one(mine, url)
            rekey.append((keep, name))
            redundant += [(hook, keep) for hook in rest]
    for name in sorted(missing.values()):
        plan = [(f"re-keys hook {hook['id']}" if routes.serves_route_url(hook["config"]["url"], urls[name])
                 else f"moves hook {hook['id']} from {hook['config']['url']} and re-keys it")
                for hook, hooked in rekey if hooked == name]
        print(f"  route {name}: {'would recreate' if dry_run else 'recreating'} from the loop "
              f"config (new secret)" + (f"; {plan[0]}" if plan else "; no repo hook points at it"))
    for hook, keep in redundant:
        print(f"  {_redundant_hook_line(loop, hook, keep)}")
    if dry_run:
        return 0, len(redundant)
    written: list[str] = []
    try:
        written = list(cli_common._install_routes(loop, roles=tuple(roles)).values())
        for hook, name in rekey:
            # A hook that already reaches the route keeps its own URL (a query string, say);
            # one left elsewhere is moved to the route's URL.
            url = (hook["config"]["url"] if routes.serves_route_url(hook["config"]["url"], urls[name])
                   else urls[name])
            cli_common._patch_hook_url(loop, hook["id"], url, token_login, require_secret=True,
                            insecure_ssl=hook["config"].get("insecure_ssl"))
            print(f"  hook {hook['id']} re-keyed for {name}")
        route_intent.record_live(loop, written)
    except Exception as exc:
        try:
            routes.restore_entries({name: None for name in written})
            route_intent.forget(loop, written)
        except Exception as rollback_exc:
            print(f"ROLLBACK FAILED: {rollback_exc} — inspect routes {names} manually")
        print(f"route recreation FAILED: {exc}; the recreated routes were taken back out — a hook "
              "already re-keyed now signs with a secret no route holds: re-run this command")
        print(f"  fix: {cli_common._hook_fix(loop, exc, token_login)}")
        return 2, 0
    for name in written:
        print(f"  route {name} recreated")
    return (0 if cli_common._install_shims(loop, report=False) else 2), len(redundant)


def _diverged(loop: dict, *, rewritten=(), header: str = "") -> bool:
    """Say so when a route still is not what the config installs, even after a push: never
    report success over a route the gateway runs under another profile or gate.

    ``rewritten`` names the routes this apply is about to write from the config: a dry run passes
    them, so it prints exactly what the real apply will still find afterwards."""
    left = {name: value for name, value in gate_shims.divergence(loop, contract=True).items()
            if name not in set(rewritten)}
    if left and header:
        print(header)
    for name, (detail, fix, _status) in left.items():
        print(f"  ⚠️ route {name}: {detail} — fix: {fix}")
    return bool(left)


def _set_adjudication(args, loop: dict) -> int:
    """``set --adjudicator-profile P`` / ``--adjudicator-route NAME`` / ``--adjudicator off``.

    On: the route is written the way ``init --adjudicator-route`` writes it (ownership check,
    intent record, shim) and the ``adjudicator`` block with it. Off: the route, its intent record
    and its shim go, and so does the block; the ledger's breach markers and rulings stay.
    A failed route write puts the config and the registry back.
    """
    path = config.config_dir() / f"{loop['id']}.json"
    current = dict(loop.get("adjudicator") or {})
    old_route = str(current.get("route") or "")
    if getattr(args, "adjudicator", None) == "off":
        if getattr(args, "adjudicator_profile", None) or getattr(args, "adjudicator_route", None):
            print("refused: --adjudicator off cannot be combined with --adjudicator-profile "
                  "or --adjudicator-route")
            return 2
        if not old_route:
            print(f"[{loop['id']}] adjudication already off")
            return 0
        seats = {seat: dict(cfg) for seat, cfg in loop["seats"].items()}
        adj_seat = seats.pop("adjudicator", None) or {}
        adj_seat.pop("login", None)
        if adj_seat:
            seats["adjudicator"] = adj_seat
        try:
            updated = config.normalize({**loop, "adjudicator": {}, "seats": seats})
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        previous_config = path.read_bytes()
        previous_route = routes.route(old_route)
        try:
            cli_common._write_config(updated)
            routes.remove_route(old_route)
            route_intent.forget(loop, [old_route])
        except Exception as exc:
            try:
                routes.restore_entries({old_route: previous_route})
                cli_common._restore_config(path, previous_config)
            except Exception as rollback_exc:
                print(f"ROLLBACK FAILED: {rollback_exc} — inspect {path} and route {old_route}")
                return 2
            print(f"adjudication off FAILED: {exc}; config and route restored")
            return 2
        shims = gate_shims.remove({**loop, "seats": {}, "triage": {}, "observer": {}},
                                  [updated, *[other for other in config.all_loops()
                                              if other["id"] != loop["id"]]])
        print(f"[{loop['id']}] adjudication off: route {old_route} removed — a spent cap now "
              "writes only the breach marker (existing markers and rulings stay in the ledger)")
        for line in shims:
            print(f"  {line}")
        return 0

    d = settings_form.settings_defaults(cli_common._SETTINGS)
    profile = (str(getattr(args, "adjudicator_profile", None) or "").strip()
               or str(current.get("profile") or "") or d["adjudicator_profile"] or "default")
    route = (str(getattr(args, "adjudicator_route", None) or "").strip() or old_route
             or cli_common.routes_for(loop)["adjudicator"])
    try:
        updated = config.normalize({**loop, "adjudicator": {**current, "route": route,
                                                            "profile": profile}})
        config.webhook_host(updated.get("host"), required=True)
        credentials.verify_seats(updated, {"adjudicator"})
        cli_common._verify_routes(updated, {"adjudicator"})
        gate_shims.install(updated, dry_run=True, report=False)
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    if (updated.get("adjudicator") or {}) == current and routes.route(route):
        print(f"[{loop['id']}] adjudication already on (route {route}, profile {profile})")
        return 0
    previous_config = path.read_bytes()
    names = {route, *([old_route] if old_route else [])}
    previous_routes = {name: routes.route(name) for name in names}
    try:
        cli_common._write_config(updated)
        cli_common._install_routes(updated, roles=("adjudicator",))
        route_intent.record_live(updated, [route])
    except Exception as exc:
        try:
            routes.restore_entries(previous_routes)
            cli_common._restore_config(path, previous_config)
        except Exception as rollback_exc:
            print(f"ROLLBACK FAILED: {rollback_exc} — inspect {path} and route {route}")
            return 2
        print(f"adjudication install FAILED: {exc}; config and route restored")
        return 2
    print(f"[{loop['id']}] adjudication on: profile {profile}, route {route} written")
    if old_route and old_route != route:
        try:
            route_intent.forget(updated, [old_route])
            routes.remove_route(old_route)
            print(f"  old route {old_route} removed")
        except (OSError, ValueError) as exc:
            print(f"warning: old adjudicator route {old_route!r} remains; remove it manually: {exc}")
    shims_ok = cli_common._install_shims(updated)
    print("  no repo hook is needed: the loop wakes the adjudicator itself when the cap is spent")
    print(f"  next: hermes dk doctor --loop {loop['id']}")
    return 0 if shims_ok else 1


def cmd_set(args) -> int:
    """Change a loop's settings in place, through the same validation ``init`` uses.

    Seat prompts are rendered from the payload at fire time; observer destination changes also
    reconcile the delivery-only route before updating config. The rails still apply — ``concurrency``
    above 1 without a ``clone`` is refused here exactly as it is at init, because a parallel run
    that cannot be isolated would share a checkout.
    """
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        # The one repair a verb can make to a file the loader refuses: a missing reader, named
        # here with --read-token (and its --token). Any other defect still refuses.
        try:
            loop = config.load_id_for_reader_repair(args.loop,
                                                    str(getattr(args, "read_token", "") or "").strip())
        except config.ConfigError as other:
            exc = other
            loop = None
        if loop is None:
            print(f"no such loop: {exc}")
            return 2
        print(f"repairing {args.loop}: it has no read_token; setting the reader named by --read-token")

    adj_done = False
    if (getattr(args, "adjudicator_profile", None) or getattr(args, "adjudicator_route", None)
            or getattr(args, "adjudicator", None)):
        rc = _set_adjudication(args, loop)
        if rc:
            return rc
        adj_done = True
        loop = config.load_id(args.loop)

    host = args.host
    if host is not None:
        # A blank or whitespace host is not "unchanged": it would strip the loop of the origin
        # its hooks and routes are matched by. Say so, and validate a real one here, by name.
        host = host.strip()
        try:
            if not host:
                raise config.ConfigError("--host needs your gateway origin (https://…); a blank "
                                         "host would orphan the loop's hooks — to take the loop "
                                         "down use `uninstall`")
            config.webhook_host(host, required=True)
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
    wanted = {"concurrency": args.concurrency, "cap": args.cap, "base": args.base,
              "clone": args.clone, "grace_min": args.grace_min,
              "marker_grace_min": args.marker_grace_min, "ttl_min": args.ttl_min,
              "inflight_ttl_min": args.inflight_ttl_min, "host": host,
              "turn_budget_s": getattr(args, "turn_budget", None),
              "attribution": cli_common._attribution_arg(args, None),
              "review_after_ci": cli_common._on_off(args, "review_after_ci", None),
              "fix_ci": cli_common._on_off(args, "fix_ci", None),
              "fixer_scope_lock": cli_common._on_off(args, "fixer_scope_lock", None),
              "review_only_update": cli_common._on_off(args, "review_only_update", None)}
    if (wanted["review_only_update"] and not loop.get("review_only_update")
            and not getattr(args, "acknowledge_branch_push", False)):
        print(BRANCH_PUSH_REFUSAL)
        return 2
    changes = {k: v for k, v in wanted.items()
               if v is not None and v != "" and v != loop.get(k)}
    if getattr(args, "required_check", None) is not None or getattr(args, "no_required_checks",
                                                                     False):
        try:
            names = config.check_required_checks(
                [] if args.no_required_checks else args.required_check, "--required-check")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if names != (loop.get("required_checks") or []):
            changes["required_checks"] = names
    if getattr(args, "human_path", None) is not None or getattr(args, "no_human_paths", False):
        try:
            names = config.check_human_paths(
                [] if args.no_human_paths else args.human_path, "--human-path")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if names != (loop.get("human_paths") or []):
            changes["human_paths"] = names
    if getattr(args, "generated", None) is not None or getattr(args, "no_generated", False):
        try:
            names = config.parse_generated(
                [] if args.no_generated else args.generated, "--generated")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if names != (loop.get("generated") or []):
            changes["generated"] = names
    if getattr(args, "no_shadow", False) or getattr(args, "shadow_profile", None) is not None \
            or getattr(args, "shadow_every", None) is not None \
            or getattr(args, "shadow_escalate", None) is not None:
        from ..config import settings_form
        try:
            shadow = settings_form.shadow_block(
                loop.get("shadow"), "off" if args.no_shadow else args.shadow_profile,
                args.shadow_every, "--shadow-profile/--shadow-every/--shadow-escalate",
                escalate=getattr(args, "shadow_escalate", None))   # "on" with --no-shadow: refused
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if shadow is not None and shadow != (loop.get("shadow") or {}):
            changes["shadow"] = shadow
    if getattr(args, "review_only", None) is not None or getattr(args, "no_review_only", False):
        try:
            names = config.check_review_only([] if args.no_review_only else args.review_only,
                                             loop, "--review-only")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if names != (loop.get("review_only") or []):
            changes["review_only"] = names
    if getattr(args, "ci_fix_cap", None) is not None:
        try:
            value = (None if str(args.ci_fix_cap).strip() in ("0", "")
                     else config.check_ci_fix_cap(args.ci_fix_cap, "--ci-fix-cap"))
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if value != loop.get("ci_fix_cap"):
            changes["ci_fix_cap"] = value
    for key in ("review_only_cap", "review_only_daily"):
        wanted = getattr(args, key, None)
        if wanted is None:
            continue
        try:
            value = None if str(wanted).strip() in ("0", "") else config._check_review_only_limit(
                wanted, key, f"--{key.replace('_', '-')}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if value != loop.get(key):
            changes[key] = value
    # '' is a real value here: it clears the check.
    if getattr(args, "fixer_check", None) is not None:
        try:
            check = config.check_fixer_check(args.fixer_check, "--fixer-check")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        if check != (loop.get("fixer_check") or ""):
            changes["fixer_check"] = check

    seats = {seat: dict(cfg) for seat, cfg in loop["seats"].items()}
    seat_changes = {}

    # The adjudicator's optional comment identity and the reader. `set` maps only *their*
    # credentials: the seats' token files move through the settings form and `apply`, which owns
    # the in-flight rules.
    tokens = dict(loop.get("tokens") or {})
    adj_before = config.adjudicator_login(loop)
    adj_wanted = getattr(args, "adjudicator_login", None)
    adj_after = adj_before if adj_wanted is None else adj_wanted.strip()
    read_before = str(loop.get("read_token") or "")
    read_wanted = getattr(args, "read_token", None)
    read_after = read_before if read_wanted is None else read_wanted.strip()
    if read_wanted is not None and not read_after:
        print("refused: --read-token needs a login — the gates cannot read GitHub as nobody")
        return 2
    for pair in getattr(args, "token", None) or []:
        split = _split_token_pair(pair)
        if split is None:
            return 2
        login, path = split
        owner = next((name for name in (adj_after, read_after if read_wanted is not None else "")
                      if name and login.lower() == name.lower()), "")
        if not owner and _is_seat_or_reader_login(loop, login):
            print(f"refused: `set --token` maps the token file of the login named by --read-token "
                  f"or --adjudicator-login, or of an extra login such as a hook admin "
                  f"(--admin-token); {login!r} is a seat or the current reader — seat token files "
                  "move through the plugin settings and `apply`, and a reader moves with "
                  "--read-token")
            return 2
        try:
            credentials.check_token_file(path, f"--token {login}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        for key in [k for k in tokens if str(k).lower() == login.lower()]:
            del tokens[key]
        tokens[owner or login] = str(pathlib.Path(path.strip()).expanduser())
    # gh looks a login's file up by its exact key: keep the reader spelled as its mapping is.
    read_after = next((str(k) for k in tokens if str(k).lower() == read_after.lower()), read_after)
    tokens_changed = tokens != (loop.get("tokens") or {})
    adj_changed = adj_after != adj_before or tokens_changed
    read_changed = (read_after != read_before
                    or cli_common._token_ref({"tokens": tokens}, read_after) != cli_common._token_ref(loop, read_after))
    if adj_after != adj_before:
        adj_seat = dict(seats.get("adjudicator") or {})
        if adj_after:
            if not (loop.get("adjudicator") or {}).get("route"):
                print("refused: this loop has no adjudicator route, so nothing rules — an "
                      "adjudicator login would never post; turn adjudication on first with "
                      f"`hermes dk set --loop {loop['id']} --adjudicator-profile PROFILE`")
                return 2
            adj_seat["login"] = adj_after
        else:
            adj_seat.pop("login", None)
        if adj_seat:
            seats["adjudicator"] = adj_seat
        else:
            seats.pop("adjudicator", None)
    for seat, value in (("reviewer", args.reviewer_concurrency), ("fixer", args.fixer_concurrency)):
        if value is not None and value != config.seat_concurrency(loop, seat):
            seats[seat]["concurrency"] = value
            seat_changes[seat] = value
    budget_changes = {}
    for seat, value in (("reviewer", getattr(args, "reviewer_turn_budget", None)),
                         ("fixer", getattr(args, "fixer_turn_budget", None))):
        if value is not None and value != (seats[seat].get("turn_budget_s")):
            budget_changes[seat] = (config.turn_budget(loop, seat), value)
            seats[seat]["turn_budget_s"] = value
    # A daily turn cap per seat (#219); 0 removes it. normalize refuses anything else.
    for seat, value in (("reviewer", getattr(args, "reviewer_daily_turns", None)),
                         ("fixer", getattr(args, "fixer_daily_turns", None))):
        if value is None or (value or None) == config.seat_daily_turns(loop, seat):
            continue
        budget_changes[f"{seat} daily turns"] = (config.seat_daily_turns(loop, seat), value or None)
        if value:
            seats[seat]["daily_turns"] = value
        else:
            seats[seat].pop("daily_turns", None)
    # A daily token budget per seat (#480); 0 removes it. normalize refuses anything else.
    for seat, value in (("reviewer", getattr(args, "reviewer_daily_tokens", None)),
                         ("fixer", getattr(args, "fixer_daily_tokens", None))):
        if value is None or (value or None) == config.seat_daily_tokens(loop, seat):
            continue
        budget_changes[f"{seat} daily tokens"] = (config.seat_daily_tokens(loop, seat),
                                                  value or None)
        if value:
            seats[seat]["daily_tokens"] = value
        else:
            seats[seat].pop("daily_tokens", None)
    # The shadow seat's limits (#875, #218): `0` removes one (the default again). normalize
    # refuses anything out of range.
    for key, flag, now in (("daily_tokens", "shadow_daily_tokens", config.seat_daily_tokens),
                           ("daily_turns", "shadow_daily_turns", config.seat_daily_turns),
                           ("turn_budget_s", "shadow_turn_budget", None),
                           ("max_steps", "shadow_max_steps", None)):
        value = getattr(args, flag, None)
        before = (seats.get("shadow") or {}).get(key)
        if value is None or (value or None) == before:
            continue
        # Named so the printer below says what changed: "shadow" alone is the turn budget.
        label = "shadow" if key == "turn_budget_s" else f"shadow {key.replace('_', ' ')}"
        shown = config.turn_budget(loop, "shadow") if key == "turn_budget_s" else before
        # With no seat value left, the shadow turn runs on the loop default as this same `set`
        # leaves it: `changes` holds a `--turn-budget` given alongside.
        after = (value or config.turn_budget({**loop, **changes, "seats": {}}, "shadow")
                 if key == "turn_budget_s" else value or None)
        budget_changes[label] = (shown, after)
        shadow_seat = seats.setdefault("shadow", {})
        if value:
            shadow_seat[key] = value
        else:
            shadow_seat.pop(key, None)
        if not shadow_seat:
            seats.pop("shadow", None)
    # Agent steps per turn (#271); 0 goes back to the seat's default. An issue fix takes the
    # fixer's value. normalize refuses anything outside the range.
    for seat, value in (("reviewer", getattr(args, "reviewer_max_steps", None)),
                         ("fixer", getattr(args, "fixer_max_steps", None))):
        if value is None or (value or config.DEFAULT_MAX_STEPS[seat]) == config.max_steps(loop, seat):
            continue
        budget_changes[f"{seat} max steps"] = (config.max_steps(loop, seat),
                                               value or config.DEFAULT_MAX_STEPS[seat])
        if value:
            seats[seat]["max_steps"] = value
        else:
            seats[seat].pop("max_steps", None)

    # The observer is a nested block, so it is collected the same way the seats are: flags the
    # operator did not pass leave the existing answer alone, and a flag that means "drop it"
    # (--observer-disable) is explicit rather than implied by an empty string.
    observer_cfg = dict(loop.get("observer") or {})
    if args.observer_disable:
        observer_cfg = {}
    if args.observer_route:
        observer_cfg["route"] = args.observer_route
    if args.observer_profile:
        observer_cfg["profile"] = args.observer_profile
    if args.observer_profile and not observer_cfg.get("route"):
        observer_cfg["route"] = f"{loop['id']}-observe"
    if args.observer_deliver:
        observer_cfg["deliver"] = args.observer_deliver
    if args.observer_events is not None:
        observer_cfg["events"] = args.observer_events
    if args.observer_digest_min is not None:
        observer_cfg["digest_min"] = args.observer_digest_min
    weekly = getattr(args, "observer_weekly_report", None)
    if weekly is not None:
        if weekly.strip():
            try:
                observer_cfg["weekly_report"] = config.check_weekly_report(weekly)
            except ValueError as exc:
                print(f"refused: --observer-weekly-report: {exc}")
                return 2
        else:
            observer_cfg.pop("weekly_report", None)   # blank = no weekly report
    for key in ("urgent_route", "urgent_profile", "urgent_deliver"):
        value = getattr(args, f"observer_{key}", None)
        if value is not None:
            if value:
                observer_cfg[key] = value
            else:
                observer_cfg.pop(key, None)        # blank = back to one feed
    if args.observer_mute or args.observer_unmute:
        if not observer_cfg.get("route"):
            # Muting something that does not exist would write a feed with nowhere to go — a
            # configuration error the operator would only discover by finding no notices.
            print("no observer feed on this loop — name one with --observer-route / "
                  "--observer-profile first")
            return 2
    if args.observer_mute:
        observer_cfg["mute"] = True
    if args.observer_unmute:
        observer_cfg["mute"] = False

    if (not changes and not seat_changes and not budget_changes and not adj_changed
            and not read_changed
            and observer_cfg == (loop.get("observer") or {})):
        if adj_done:
            return 0
        print("nothing to change — pass at least one setting "
              "(--concurrency, --reviewer-concurrency, --fixer-concurrency, --cap, --clone, "
              "--turn-budget, "
              "--observer-profile, ...)")
        return 0

    try:
        updated = config.normalize({**loop, **changes, "seats": seats, "observer": observer_cfg,
                                    "tokens": tokens, "read_token": read_after})
        if read_changed:
            # The same rules init applies to the reader: mapped, its file present and private,
            # and its own account (the four-identity rule).
            if not cli_common._token_ref(updated, read_after):
                raise config.ConfigError(
                    f"no token file mapped for the reader {read_after!r} — add "
                    f"--token {read_after}=/path/to/pat")
            credentials.check_token_file(cli_common._token_ref(updated, read_after),
                                    f"token file for the reader {read_after!r}")
            credentials.verify_credentials(updated, {"read"})
        if adj_changed and config.adjudicator_login(updated):
            # The same file-level rules init applies, plus: the file must exist and be private.
            credentials.verify_adjudicator_token(updated)
            credentials.verify_credentials(updated)
        cli_common._observer_check(updated)
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2

    shims_ok = True
    before = loop.get("observer") or {}
    after = updated.get("observer") or {}
    # Disabling stops new notices and removes the route, but leaves the outbox intact.
    # Keep its former destination as a tombstone: otherwise re-enabling at a new
    # destination (or host) could forward a queued private PR link there.
    previous = loop.get("observer_disabled") or {}
    bound = before or previous
    keys = ("route", "profile", "deliver")
    old_target = {k: bound.get(k) for k in keys}
    old_target["host"] = previous.get("host") if previous else loop.get("host")
    new_target = {k: after.get(k) for k in keys}
    new_target["host"] = updated.get("host")
    destination_changed = any(before.get(k) != after.get(k)
                              for k in ("route", "profile", "deliver"))
    urgent_keys = ("urgent_route", "urgent_profile", "urgent_deliver")
    urgent_changed = any(before.get(k) != after.get(k) for k in urgent_keys) or (
        bool(after.get("urgent_route")) and
        any(before.get(k) != after.get(k) for k in ("profile", "deliver")))
    if bound and after and (old_target != new_target or (urgent_changed and before)):
        try:
            outstanding = observer.unsettled(state_mod.state_for(loop))
        except (OSError, ValueError) as exc:
            print(f"observer ledger cannot be checked — destination unchanged: {exc}")
            return 2
        if outstanding:
            print(f"refused: {outstanding} observer notice(s) are unsettled; retry/resolve "
                  "them before changing the route, profile, delivery or host")
            return 2
    if after:
        updated.pop("observer_disabled", None)
    elif before:
        updated["observer_disabled"] = old_target
    if destination_changed and after:
        name = after["route"]
        reserved = {cfg.get("route") for cfg in loop["seats"].values()}
        reserved.add((loop.get("adjudicator") or {}).get("route"))
        if name in reserved:
            print("refused: observer route must not replace a seat or adjudicator route")
            return 2
        existing = routes.route(name)
        if existing and name != before.get("route"):
            print(f"refused: route {name!r} already exists and is not this observer's route")
            return 2
        try:
            host = config.webhook_host(updated.get("host"), required=True)
            written = routes.new_route(name, profile=config.seat_profile(updated, "observer"),
                                       prompt=prompts.OBSERVER,
                                       events=["pull_request"], script="observe.py",
                                       deliver=after["deliver"], deliver_only=True, host=host,
                                       description=f"{updated['repo']} — read-only observer feed")
        except (OSError, ValueError, config.ConfigError) as exc:
            print(f"observer route could not be reconciled; loop config unchanged: {exc}")
            return 2
        try:
            route_intent.record(updated, {name: written})
        except (OSError, ValueError) as exc:
            # Without the record, self-heal would put the *old* route back: undo the route too.
            try:
                routes.restore_entries({name: existing})
            except (OSError, ValueError) as rollback_exc:
                print(f"ROLLBACK FAILED: {rollback_exc} — inspect route {name!r} manually")
            print(f"observer route intent could not be recorded; loop config unchanged: {exc}")
            return 2
        shims_ok = cli_common._install_shims(updated)
    if urgent_changed and after.get("urgent_route"):
        urgent = after["urgent_route"]
        reserved = {cfg.get("route") for cfg in loop["seats"].values()}
        reserved |= {(loop.get("adjudicator") or {}).get("route"), after.get("route")}
        existing = routes.route(urgent)
        if urgent in reserved or (existing and urgent != before.get("urgent_route")):
            print(f"refused: route {urgent!r} is already in use and is not this observer's "
                  "urgent route")
            return 2
        try:
            host = config.webhook_host(updated.get("host"), required=True)
            written = routes.new_route(
                urgent, profile=config.seat_profile(updated, "observer_urgent"),
                prompt=prompts.OBSERVER, events=["pull_request"], script="observe.py",
                deliver=after.get("urgent_deliver") or after["deliver"], deliver_only=True,
                host=host, description=f"{updated['repo']} — read-only observer feed: urgent "
                                       "notices only")
            route_intent.record(updated, {urgent: written})
        except (OSError, ValueError, config.ConfigError) as exc:
            print(f"observer urgent route could not be reconciled; loop config unchanged: {exc}")
            return 2
        shims_ok = cli_common._install_shims(updated) and shims_ok
    path = cli_common._write_config(updated)
    if before.get("urgent_route") and before["urgent_route"] != after.get("urgent_route"):
        try:
            route_intent.forget(updated, [before["urgent_route"]])
            routes.remove_route(before["urgent_route"])
        except (OSError, ValueError) as exc:
            print(f"warning: old urgent route {before['urgent_route']!r} remains; "
                  f"remove it manually: {exc}")
    if destination_changed and before.get("route") and before["route"] != after.get("route"):
        try:
            route_intent.forget(updated, [before["route"]])
            routes.remove_route(before["route"])
        except (OSError, ValueError) as exc:
            print(f"warning: old observer route {before['route']!r} remains; remove it manually: {exc}")
    for key, value in changes.items():
        print(f"  {key}: {loop.get(key)!r} → {value!r}")
    for seat, value in seat_changes.items():
        was = config.seat_concurrency(loop, seat)
        print(f"  {seat} concurrency: {was} → {value}  (this seat only)")
    for seat, (was, value) in budget_changes.items():
        if seat.endswith(" daily turns"):
            print(f"  {seat}: {was or 'no cap'} → {value or 'no cap'}  (this seat only)")
        elif seat.endswith(" daily tokens"):
            print(f"  {seat}: {was or 'no budget'} → {value or 'no budget'}  (this seat only)")
        elif seat.endswith(" max steps"):
            print(f"  {seat}: {was} → {value}  (this seat only; an issue fix takes the fixer's)")
        else:
            print(f"  {seat} turn budget: {was}s → {value}s  (this seat only)")
    if read_after != read_before:
        print(f"  read_token: {read_before or '(none)'} → {read_after}")
    if read_after and cli_common._token_ref(loop, read_after) != cli_common._token_ref(updated, read_after):
        print(f"  reader token file ({read_after}): {cli_common._token_ref(loop, read_after) or '(none)'}"
              f" → {cli_common._token_ref(updated, read_after)}")
    if adj_after != adj_before:
        shown = adj_after or "(none — rulings go to the operator only)"
        print(f"  adjudicator login: {adj_before or '(none)'} → {shown}")
    if adj_after and cli_common._token_ref(loop, adj_after) != cli_common._token_ref(updated, adj_after):
        print(f"  adjudicator token file ({adj_after}): {cli_common._token_ref(loop, adj_after) or '(none)'}"
              f" → {cli_common._token_ref(updated, adj_after)}")
    if updated.get("observer") != (loop.get("observer") or {}):
        print(f"  observer: {observer.describe(loop.get('observer') or {})} → "
              f"{observer.describe(updated.get('observer') or {})}")
    print(f"loop config updated: {path}")
    if updated.get("host") != loop.get("host"):
        print(f"  next: `hermes dk apply --loop {updated['id']}` "
              "(rewrites the routes' origin; add --hooks to repoint the hooks)")

    if "turn_budget_s" in changes:
        for seat in ("reviewer", "fixer"):
            if (updated["seats"].get(seat) or {}).get("turn_budget_s") is not None:
                print(f"  note: {seat} has its own turn budget "
                      f"({updated['seats'][seat]['turn_budget_s']}s) — the loop default does not "
                      "apply to it")
    if "turn_budget_s" in changes or any(not key.endswith((" daily turns", " max steps"))
                                         for key in budget_changes):
        print("  turn budget now: " + cli_common._budget_line(updated)
              + "   (queued turns keep the budget they were enqueued with)")

    for line in cli_common._parallel_lines(updated):
        # The seat notes only when the loop default moved: that is when they explain something.
        if "concurrency" in changes or not line.startswith("note:"):
            print(f"  {line}")
    return 0 if shims_ok else 1


def cmd_apply(args) -> int:
    """``apply``, then the explicit extras it was asked for (``--hooks``, ``--watchdog-shim``),
    after the routes are what the config says — their URLs are what the hooks must post to."""
    rc = _apply(args)
    if rc == 2 or not (getattr(args, "hooks", False) or getattr(args, "watchdog_shim", False)):
        return rc
    loop = config.load_id(args.loop)
    if getattr(args, "watchdog_shim", False):
        if args.dry_run:
            print(f"  would write the watchdog shim: {doctor.shim_path()}")
        else:
            try:
                print(f"  watchdog shim written: {cli_common._write_watchdog_shim()}")
            except OSError as exc:
                print(_shim_refusal(exc))
                return 1
    if getattr(args, "hooks", False):
        rc = max(rc, _ensure_hooks(loop, getattr(args, "admin_token", "") or None, args.dry_run))
    return rc


# Settings-form keys that live inside a loop section, which ``_apply`` compares as it does the
# top-level ones (triage.fix_daily_turns is compared as the issue fixer's daily turns).
_NESTED_FORM_KEYS = (("triage", "auto_fix_labels"), ("triage", "auto_fix_daily"),
                     ("observer", "weekly_report"))


def _apply(args) -> int:
    """Make a loop match the plugin settings — one push, with the diff printed.

    Push, not subscription: a running loop whose numbers changed under it is exactly the kind of
    thing nobody can debug at 2am. The same validation as ``init``/``set`` still applies, so a
    settings form asking for two reviews at once without a clone path is refused here too.

    An identity change is *staged* rather than half-applied: the loop config and the routes whose
    URL carries the old profile move together, and a change that would land while a seat has a run
    in flight is refused unless the operator says otherwise out loud.
    """
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2

    try:
        # Token-file paths first: a bad path is refused by name before anything is computed.
        credentials.verify_token_settings(cli_common._SETTINGS)
        updated = config.normalize(settings_form.apply_settings(loop, cli_common._SETTINGS))
    except config.ConfigError as exc:
        print(f"settings refused: {exc}")
        return 2

    # The four-identity rule holds for the config apply would leave behind, whether or not this
    # push moves an identity: "nothing changed" must not endorse a reader that is also a seat.
    problem = credentials.reader_problem(updated)
    if problem:
        print(f"settings refused: {updated['id']}: {problem} — {config.FOUR_IDENTITY_RULE}")
        print(f"fix: {credentials.reader_fix(updated)}")
        return 2
    reader = str(updated.get("read_token") or "")
    if reader.casefold() not in {str(k).casefold() for k in updated.get("tokens") or {}}:
        # doctor fails this loop; apply must not report success over it either.
        print(f"settings refused: {updated['id']}: read_token {reader!r} has no entry in "
              "'tokens' — the gates read GitHub as that login and have no file to read it from")
        print(f"fix: hermes dk set --loop {updated['id']} --read-token {reader} "
              f"--token {reader}=/path/to/pat")
        return 2

    identity, touched = _seat_diffs(loop, updated)
    # The installed registry can drift independently of the loop and the form. Repair those
    # routes through the same ownership, seat and in-flight preflight as an identity push.
    binds = _route_binds(updated, set(cli_common._routes_of(updated)))
    # A route installed by an older release (the pre-#21 breach route on gate_reviewer.py) is
    # repaired here too: `init` refuses an existing loop, so apply is the only reconcile path.
    repairs = _stale_scripts(updated)
    drifted = _drifted_routes(updated)
    rebinding = touched | set(binds) | set(repairs) | set(drifted)
    try:
        # Validate what this apply would *write*: a loop that predates the seat checks keeps
        # loading, but a seat this push moves must be one that can actually run.
        credentials.verify_seats(updated, rebinding)
        adj = config.adjudicator_login(updated)
        if adj and (adj != config.adjudicator_login(loop)
                    or cli_common._token_ref(loop, adj) != cli_common._token_ref(updated, adj)):
            credentials.verify_adjudicator_token(updated)
            credentials.verify_credentials(updated)
        if rebinding:
            cli_common._verify_routes(updated, rebinding)
        # Refuses a foreign file (#105) — only over what this apply writes: the config's pairs. A
        # route the registry holds elsewhere is what apply is about to rebind away, so it never
        # blocks; disagreements left afterwards are reported by ``_diverged``.
        shim_pairs = gate_shims.wanted(updated)
        shim_lines = gate_shims.install(updated, dry_run=True, report=False, pairs=shim_pairs)
    except config.ConfigError as exc:
        print(f"settings refused: {exc}")
        return 2
    # The gate shims are not a setting and move nothing, so apply writes them up front, even when
    # everything else already matches: a loop whose gates the gateway cannot find drops every event.
    if shim_lines and args.dry_run:
        for line in shim_lines:
            print(f"  {line}")
    elif shim_lines and not cli_common._install_shims(updated, report=False, pairs=shim_pairs):
        return 2
    missing = {role: name for role, name in cli_common._routes_of(updated).items() if not routes.route(name)}
    leftover_hooks = 0
    if missing and getattr(args, "recreate_routes", False):
        rc, leftover_hooks = _recreate_routes(updated, missing, dry_run=args.dry_run,
                                              token_login=getattr(args, "admin_token", "") or None)
        if rc:
            return rc

    changes = []
    for key in ("cap", "base", "host", "grace_min", "ttl_min", "inflight_ttl_min",
                "turn_budget_s", "attribution", "fixer_check", "review_after_ci",
                "fix_ci", "fixer_scope_lock", "ci_fix_cap", "review_only_update", "required_checks", "human_paths",
                "generated", "review_only", "review_only_cap", "review_only_daily", "shadow"):
        if updated.get(key) != loop.get(key):
            changes.append((key, loop.get(key), updated.get(key)))
    # The form also pushes triage.auto_fix_* and observer.weekly_report (#781): compare them, or a
    # push that moves only those is discarded as "already matches" and never written.
    for section, key in _NESTED_FORM_KEYS:
        was, now = (loop.get(section) or {}).get(key), (updated.get(section) or {}).get(key)
        if was != now:
            changes.append((f"{section}.{key}", was, now))
    if (updated.get("clone") or "") != (loop.get("clone") or ""):
        changes.append(("clone", loop.get("clone") or "(none)", updated.get("clone") or "(none)"))
    for seat in ("reviewer", "fixer"):
        was, now = config.seat_concurrency(loop, seat), config.seat_concurrency(updated, seat)
        if was != now:
            changes.append((f"{seat} concurrency", was, now))

    for seat in ("reviewer", "fixer"):
        was, now = config.max_steps(loop, seat), config.max_steps(updated, seat)
        if was != now:
            changes.append((f"{seat} max steps", was, now))
    for seat in ("reviewer", "fixer", "shadow"):
        was, now = config.seat_daily_tokens(loop, seat), config.seat_daily_tokens(updated, seat)
        if was != now:
            changes.append((f"{seat} daily tokens", was, now))
    was = config.seat_daily_turns(loop, "issue_fixer")
    now = config.seat_daily_turns(updated, "issue_fixer")
    if was != now:
        changes.append(("fix daily turns", was, now))

    missing_routes = sorted(name for role, name in cli_common._routes_of(updated).items()
                            if role in touched and not routes.route(name))

    if not changes and not identity and not binds and not repairs and not drifted:
        # Never "matches" over a route the registry holds differently (#112 review): the loop
        # config can agree with the settings while the gateway serves something else.
        left = _diverged(updated, header=f"[{loop['id']}] the loop config matches the plugin "
                                         "settings, but the route registry does not:")
        if not left:
            print(f"[{loop['id']}] already matches the plugin settings")
        duplicates, slashed = [], []
        # With --hooks, the hooks are reconciled after this by _ensure_hooks — the same listing,
        # the same keep-one rule, and a repoint for a hook this read-only check can only refuse.
        if not args.dry_run and not getattr(args, "hooks", False):
            token = getattr(args, "admin_token", "") or None
            try:        # nothing else moves: this reads, to name two hooks on one route URL and
                        # find one whose URL differs only by a trailing slash (a gateway 404)
                slashed, duplicates = _hook_moves(loop, updated, {}, token, check_only=True)
            except cli_common.HookAccessError as exc:
                print(f"  (repo hooks not checked: {exc})")
            except config.ConfigError as exc:
                print(f"  {exc}")       # a hook on a loop route this push cannot explain (#55)
                return 1
        for hook_id, old, new, ssl in slashed:
            try:
                cli_common._patch_hook_url(loop, hook_id, new, getattr(args, "admin_token", "") or None,
                                insecure_ssl=ssl)
            except config.ConfigError as exc:
                print(f"  hook {hook_id} NOT repointed from {old} to {new}: {exc}")
                print(f"  fix: {cli_common._hook_fix(loop, exc, getattr(args, 'admin_token', '') or None)}")
                return 2
            print(f"  hook {hook_id} → {new}   (was {old}: the gateway does not route a trailing "
                  "slash)")
        for hook, keep in duplicates:
            print(f"  {_redundant_hook_line(loop, hook, keep)}")
        return 1 if left or leftover_hooks or duplicates else 0
    if not args.dry_run and updated.get("host") != loop.get("host") and loop.get("observer"):
        try:
            outstanding = observer.unsettled(state_mod.state_for(loop))
        except (OSError, ValueError) as exc:
            print(f"observer ledger cannot be checked — host unchanged: {exc}")
            return 2
        if outstanding:
            print(f"refused: {outstanding} observer notice(s) are unsettled; retry/resolve "
                  "them before changing the host")
            return 2
    for name, was, now in list(changes) + list(identity):
        print(f"  {name}: {was} → {now}")
    for role, (name, current, target) in sorted(binds.items()):
        print(f"  route {name}: profile {current or '(blank)'} → {target}   (the URL carries "
              "the profile)")
    for role, (name, fields) in sorted(drifted.items()):
        if fields == ["enabled"]:
            print(f"  route {name}: disabled (enabled: false) → enabled   (the gateway answers 403 "
                  "to every event while it is off)")
        else:
            print(f"  route {name}: {', '.join(fields)} "
                  f"{'differs' if len(fields) == 1 else 'differ'} from what the plugin writes → "
                  "rewritten from the loop config   (secret kept)")
    for role, (name, script) in sorted(repairs.items()):
        print(f"  route {name}: script {script} → {GATE_SCRIPT[role]}   (installed by an older "
              "release)")
    for name in missing_routes:
        print(f"  route {name}: not installed — `hermes dk init` creates routes; "
              "apply will not invent one behind your back")
    if missed := [role for role, name in cli_common._routes_of(updated).items()
                  if role in touched and name in missing_routes]:
        print(f"refused: {', '.join(missed)} would move to a route that does not exist yet")
        return 2

    if args.dry_run:
        rewritten = ({bind[0] for bind in binds.values()} | {n for n, _ in repairs.values()}
                     | {n for n, _ in drifted.values()})
        if getattr(args, "recreate_routes", False):
            rewritten |= set(missing.values())
        left = _diverged(updated, rewritten=rewritten)
        try:
            hook_before = _hook_origin(loop, drifted)
            if hook_before is not loop:
                # The routes' origin moves, so their hooks move too: preview it (reads only).
                moves, redundant = _hook_moves(hook_before, updated, binds,
                                               getattr(args, "admin_token", "") or None)
                for hook_id, _old, new, _ssl in moves:
                    print(f"  hook {hook_id} would move → {new}")
                for hook, keep in redundant:
                    print(f"  {_redundant_hook_line(loop, hook, keep)}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        print("(dry run — nothing written: no loop config, no routes touched)")
        if left:
            print("  apply would exit 1: the route(s) above still disagree with the config after it")
        return 1 if left or leftover_hooks else 0

    busy = cli_common._busy_seats(loop, rebinding) if rebinding else []
    if busy and not getattr(args, "while_busy", False):
        for line in busy:
            print(f"  {line}")
        print("refused: a seat is in flight, and its live run holds the old profile and login "
              "until it ends. Let it finish, then apply again — or pass --while-busy to rebind "
              "now, knowing that run still finishes under the identity it started with.")
        return 2
    for line in busy:
        print(f"  {line}  (--while-busy: that run keeps the identity it started with)")

    # Preflight remote hooks before any local mutation. Snapshot each owned route and roll back
    # both surfaces on any failure; config is published only after route/hook readback agrees.
    try:
        hook_moves, redundant_hooks = _hook_moves(_hook_origin(loop, drifted), updated, binds,
                                                  getattr(args, "admin_token", "") or None)
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    previous = {name: routes.route(name)
                for name in ({bind[0] for bind in binds.values()} | {n for n, _ in repairs.values()}
                             | {n for n, _ in drifted.values()})}
    config_path = config.config_dir() / f"{loop['id']}.json"
    previous_config = config_path.read_bytes()
    attempted_hooks = []
    try:
        rewrite = tuple(set(binds) | set(repairs) | set(drifted))
        rebound = list(cli_common._install_routes(updated, roles=rewrite).items()) if rewrite else []
        for role, name in rebound:
            entry = routes.route(name)
            if not entry or routes.route_profile(entry) != config.seat_profile(updated, role):
                raise config.ConfigError(f"route {name} readback does not match requested profile")
            if entry.get("script") != GATE_SCRIPT[role]:
                raise config.ConfigError(f"route {name} readback does not run {GATE_SCRIPT[role]}")
            if gate_shims.contract_drift(updated, role, entry):
                raise config.ConfigError(f"route {name} readback still differs from what the "
                                         "plugin writes: "
                                         + ", ".join(gate_shims.contract_drift(updated, role, entry)))
        for hook_id, old, new, ssl in hook_moves:
            attempted_hooks.append((hook_id, old, ssl))
            cli_common._patch_hook_url(loop, hook_id, new, getattr(args, "admin_token", "") or None,
                            insecure_ssl=ssl)
        path = cli_common._write_config(updated) if changes or identity else config_path
        if rebound:
            route_intent.record_live(updated, [name for _, name in rebound])
    except Exception as exc:
        failed = []
        for hook_id, old, ssl in reversed(attempted_hooks):
            try:
                cli_common._patch_hook_url(loop, hook_id, old, getattr(args, "admin_token", "") or None,
                                insecure_ssl=ssl)
            except Exception as rollback_exc:
                failed.append(f"hook {hook_id}: {rollback_exc}")
        if previous:
            try:
                routes.restore_entries(previous)
            except Exception as rollback_exc:
                failed.append(f"routes: {rollback_exc}")
        try:
            if config_path.read_bytes() != previous_config:
                cli_common._restore_config(config_path, previous_config)
        except Exception as rollback_exc:
            failed.append(f"config: {rollback_exc}")
        try:
            config_unchanged = config_path.read_bytes() == previous_config
        except OSError:
            config_unchanged = False
        print(f"reconciliation FAILED: {exc}; " +
              ("config unchanged" if config_unchanged else "config may have changed"))
        if failed:
            print("ROLLBACK FAILED — inspect before retrying: " + "; ".join(failed))
        else:
            print("  prior routes and hook URLs restored")
        return 2
    print(f"loop config updated: {path}")
    for role, name in rebound:
        print(f"  route {name} rebound → profile {config.seat_profile(updated, role)}"
              + (f", script {GATE_SCRIPT[role]}" if role in repairs else "")
              + (f", {', '.join(drifted[role][1])} restored" if role in drifted else ""))
    for hook_id, _, new, _ssl in hook_moves:
        print(f"  hook {hook_id} → {new}")
    for hook, keep in redundant_hooks:
        print(f"  {_redundant_hook_line(loop, hook, keep)}")
    return 1 if _diverged(updated) or redundant_hooks or leftover_hooks else 0


def cmd_show(args) -> int:
    """Every setting one loop has, its effective value and where it came from (#555). Read-only."""
    from .. import show

    try:
        loop = config.load_id(args.loop)
        raw = json.loads((config.config_dir() / f"{args.loop}.json").read_text())
    except (config.ConfigError, OSError, ValueError) as exc:
        print(f"cannot show loop: {exc}")
        return 2
    rows = show.collect(loop, raw if isinstance(raw, dict) else {}, cli_common._SETTINGS)
    if args.json:
        print(json.dumps({"loop": loop["id"], "settings": rows}, indent=2, sort_keys=True))
    else:
        print(show.render(loop["id"], rows))
    return 0


def cmd_settings(args) -> int:
    """Show the plugin-level defaults — what a new loop starts from, and what ``apply`` pushes."""
    d = settings_form.settings_defaults(cli_common._SETTINGS)
    print("plugin settings (desktop: Capabilities → Plugins → diaktoros)")
    width = max(len(key) for key in settings_form.SETTINGS_SCHEMA)
    for key, spec in settings_form.SETTINGS_SCHEMA.items():
        value = d[key]
        source = "set" if str((cli_common._SETTINGS or {}).get(key, "")) not in ("", "None") else "default"
        print(f"  {key:<{width}} {str(value):<26} [{source}]  {spec['description']}")

    print("\nseat mapping (blank = not set here: a loop keeps its own answer)")
    mapping = config.seat_mapping(cli_common._SETTINGS)
    for role in config.ROUTE_ROLES:
        entry = mapping.get(role) or {}
        profile = entry.get("profile") or "(blank)"
        login = entry.get("login") or ("(blank)" if role in config.LOGIN_SETTINGS
                                       else "(blank — operator-only)")
        cell = f"login {login:<30} "
        print(f"  {role:<12} profile {profile:<20} {cell}[{'set' if entry else 'blank'}]")
        # The path only, never the file: this is where the login's PAT is read from at use time.
        print(f"  {'':<12} token file {entry.get('token_file') or '(blank)'}")
    adjudicator_note = ("the adjudicator lands only on a loop that already has an adjudicator "
                        "route; its login is optional — a fourth account the ruling is also posted "
                        "as — and it never pushes or reviews; route names stay per repository")
    print(f"  ({adjudicator_note})")

    try:
        loops = config.all_loops()
    except config.ConfigError as exc:
        loops = []
        print(f"\ncould not read every loop config: {exc}")
    if loops:
        print("\neffective mapping per loop (what each one runs as today; "
              "`apply --loop <id>` pushes the mapping above onto exactly one of them):")
        for loop in loops:
            print(f"  {loop['id']:<18} "
                  + " · ".join(_role_summary(loop, role) for role in config.ROUTE_ROLES))
            refs = cli_common._credential_lines(loop)
            if refs:
                print(f"  {'':<18} token files: " + " · ".join(refs))
    if not cli_common._SETTINGS:
        print("\nnothing set — every value above is the schema default")
    print("\napply them to a loop with: hermes dk apply --loop <id>"
          "\n(settings are defaults, not a subscription: an existing loop keeps its own seats, "
          "profiles and numbers until you apply — and blank fields above never erase them)")
    return 0


def _shim_refusal(exc: OSError) -> str:
    """Why the watchdog shim could not be written, and a chmod that can repair it: on a path that
    exists — a shim not created yet means its directory (or the nearest existing ancestor)
    refused the write (#354)."""
    failed = pathlib.Path(exc.filename or doctor.shim_path())
    target = failed
    while not target.exists() and target.parent != target:
        target = target.parent
    return (f"  refused: cannot write the watchdog shim at {failed} ({exc.strerror or exc}); "
            f"make {target} writable (e.g. `chmod u+w -- {shlex.quote(str(target))}`) and "
            "re-run `apply --watchdog-shim`")


def _triage_lines(loop: dict) -> list[str]:
    triage = loop.get("triage") or {}
    if not triage.get("route"):
        return [f"[{loop['id']}] issue triage: off"]
    seat = (loop.get("seats") or {}).get("triage") or {}
    return [f"[{loop['id']}] issue triage: on — route {triage['route']} · profile "
            f"{triage['profile']} · labels as {config.triage_login(loop)}",
            f"  authors: {', '.join(triage['authors'])}",
            f"  labels (at most {triage['max_labels']}): {', '.join(triage['labels'])}",
            f"  comment: {'allowed' if triage['comment'] else 'off (labels only)'}"
            + (f" · daily cap {seat['daily_turns']}" if seat.get("daily_turns") else ""),
            (f"  issue fixes: label {triage['fix_label']!r} by "
             f"{', '.join(triage['maintainers'])} hands an issue to the fixer"
             f" · at most {config.seat_daily_turns(loop, 'issue_fixer')} a day"
             + ("" if config.unattended_fixer_push_enabled(loop) else
                f" — OFF until unattended fixer pushes are on: "
                f"{config.fixer_push_enable_command(loop)}")) if triage.get("fix_label")
            else "  issue fixes: off (--fix-label LABEL --maintainer LOGIN turns them on)",
            _auto_offer_line(triage)]


def _auto_offer_line(triage: dict) -> str:
    """The #232 auto-offer setting, so an operator can see what a save changed (#527)."""
    labels = triage.get("auto_fix_labels") or []
    if not labels:
        return "  auto-offer: off"
    return (f"  auto-offer: {', '.join(labels)} · at most "
            f"{triage.get('auto_fix_daily') or 25} a day")


_TRIAGE_CHANGE_FLAGS = ("profile", "author", "labels", "max_labels", "comment", "login", "token",
                        "fix_label", "maintainer", "daily_turns", "fix_daily_turns",
                        "auto_fix_label", "auto_fix_daily", "admin_token")


def _triage_change_flags(args) -> list[str]:
    """Option names given on the command line that change triage (not --loop/--dry-run)."""
    return ["--" + name.replace("_", "-") for name in _TRIAGE_CHANGE_FLAGS
            if getattr(args, name, None) not in (None, "", [])]


def _split_token_pair(pair: str) -> tuple[str, str] | None:
    """Parse one ``--token login=/path/to/pat`` for `set` and `triage`: ``(login, path)`` with the
    login stripped, or ``None`` after printing the refusal (no ``=``, or an empty login that
    would write a ``tokens[""]`` entry nothing can use)."""
    if "=" not in pair:
        print(f"--token expects login=/path/to/pat, got {pair!r}")
        return None
    login, path = pair.split("=", 1)
    login = login.strip()
    if not login:
        print(f"refused: --token expects login=/path/to/pat, got {pair!r} with an empty login")
        return None
    return login, path


def _is_seat_or_reader_login(loop: dict, login: str) -> bool:
    """Whether ``login`` is a seat (reviewer, fixer) or the current reader: the accounts whose
    token files `set --token` and `triage --token` never rebind (a seat's moves through the
    plugin settings and `apply`, the reader's with `--read-token`)."""
    seat_logins = {config.seat_login(loop, seat).lower() for seat in config.SEAT_KEYS} - {""}
    return login.lower() in seat_logins | {str(loop.get("read_token") or "").lower()}


def cmd_triage(args) -> int:
    """Turn issue triage (#213) on or off for one loop, or show it.

    ``--enable`` writes the ``triage`` block, the triage route (a new secret) and its gate shim,
    and with ``--admin-token`` the repo hook on ``issues`` (paused until ``arm``). ``--disable``
    removes the route and shim, and with ``--admin-token`` the hook. Everything is validated
    before the first write, and a failed route write puts the config back.
    """
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    if not (args.enable or args.disable):
        given = _triage_change_flags(args)
        if not given:
            for line in _triage_lines(loop):
                print(line)
            return 0
        if not (loop.get("triage") or {}).get("route"):
            print(f"refused: issue triage is off on {loop['id']}, so {', '.join(given)} would "
                  "change nothing; add --enable (with --triage-profile, --author, --labels) "
                  "to turn it on")
            return 2
        args.enable = True      # triage is on: apply the flags as --enable would (#527)
    admin = args.admin_token or None
    if admin and gh.token_path(loop, admin) is None and admin.lower() not in {
            str(pair.split("=", 1)[0]).strip().lower() for pair in args.token or []}:
        print(f"refused: --admin-token {admin!r} has no token file mapped on this loop")
        return 2
    path = config.config_dir() / f"{loop['id']}.json"
    previous_config = path.read_bytes()
    old_name = (loop.get("triage") or {}).get("route") or ""
    if args.disable:
        if not old_name:
            print(f"[{loop['id']}] issue triage already off")
            return 0
        updated = config.normalize({**loop, "triage": {}})
        if args.dry_run:
            print(f"[{loop['id']}] dry run — would turn issue triage off: remove route {old_name}"
                  + (" and its repo hook" if admin else "") + "; nothing written")
            return 0
        hook_lines = []
        if admin:
            try:
                listing = cli_common._hook_listing(loop, admin, require_active=False)
                own, _other = doctor.split_route_hooks(loop, listing, [old_name], ownership=True)
                for hook in own:
                    gh.api(loop, f"/repos/{loop['repo']}/hooks/{hook['id']}", method="DELETE",
                           login=admin)
                left = [h for h in cli_common._hook_listing(loop, admin, require_active=False)
                        if h.get("id") in {hook["id"] for hook in own}]
                if left:
                    raise config.ConfigError(f"hook(s) {', '.join(str(h['id']) for h in left)} "
                                             "still present after DELETE")
                hook_lines = [f"  hook {hook['id']} deleted" for hook in own]
            except config.ConfigError as exc:
                print(f"refused: the triage hook was not removed ({exc}); nothing else changed")
                return 2
        shims = gate_shims.remove({**loop, "seats": {}, "adjudicator": {}, "observer": {}},
                                  [updated, *[other for other in config.all_loops()
                                              if other["id"] != loop["id"]]])
        cli_common._write_config(updated)
        routes.restore_entries({old_name: None})
        route_intent.forget(loop, [old_name])
        print(f"[{loop['id']}] issue triage off: route {old_name} removed")
        for line in hook_lines + [f"  {line}" for line in shims]:
            print(line)
        if not admin:
            print(f"  the repo hook posting to {old_name} (if any) is left: it now gets 404s — "
                  f"delete it on GitHub, or re-run with --admin-token LOGIN")
        return 0

    current = loop.get("triage") or {}
    labels = ([x.strip() for x in args.labels.split(",") if x.strip()] if args.labels
              else current.get("labels"))
    block = {"route": old_name or cli_common.routes_for(loop)["triage"],
             "profile": args.profile or current.get("profile") or "",
             "authors": args.author or current.get("authors") or [],
             "labels": labels or [],
             "max_labels": args.max_labels or current.get("max_labels", 3),
             "comment": (args.comment == "on") if args.comment else current.get("comment", False)}
    login = args.login or current.get("login")
    if login:
        block["login"] = login
    fix_label = current.get("fix_label") if args.fix_label is None else args.fix_label
    if fix_label:
        block["fix_label"] = fix_label
        block["maintainers"] = args.maintainer or current.get("maintainers") or []
        # The issue-fix daily cap (#247); omitted keeps the current one, 0 the default.
        fix_cap = (current.get("fix_daily_turns") if args.fix_daily_turns is None
                   else args.fix_daily_turns or None)
        if fix_cap:
            block["fix_daily_turns"] = fix_cap
        # Automatic hand-off (#232): omitted keeps the current list, an empty value clears it.
        auto = (current.get("auto_fix_labels") if args.auto_fix_label is None
                else [x for x in args.auto_fix_label if x])
        if auto:
            block["auto_fix_labels"] = auto
            auto_cap = (current.get("auto_fix_daily") if args.auto_fix_daily is None
                        else args.auto_fix_daily or None)
            if auto_cap:
                block["auto_fix_daily"] = auto_cap
    elif [x for x in args.auto_fix_label or [] if x] or args.auto_fix_daily:
        print("refused: --auto-fix-label/--auto-fix-daily need a fix label "
              "(--fix-label LABEL --maintainer LOGIN); nothing changed")
        return 2
    tokens = dict(loop.get("tokens") or {})
    for pair in args.token or []:
        split = _split_token_pair(pair)
        if split is None:
            return 2
        who, file = split
        # The same refusals as `set --token`: never a seat or the reader, and a private file.
        if _is_seat_or_reader_login(loop, who):
            print(f"refused: `triage --token` maps the token file of the triage login or of an "
                  f"extra login such as a hook admin; {who!r} is a seat or the current reader — "
                  "seat token files move through the plugin settings and `apply`, and a reader "
                  "moves with `set --read-token`")
            return 2
        try:
            credentials.check_token_file(file, f"--token {who}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        for key in [k for k in tokens if str(k).lower() == who.lower()]:
            del tokens[key]
        tokens[who] = str(pathlib.Path(file.strip()).expanduser())
    seats = dict(loop.get("seats") or {})
    if args.daily_turns is not None:
        seat = {k: v for k, v in (seats.get("triage") or {}).items() if k != "daily_turns"}
        if args.daily_turns:
            seat["daily_turns"] = args.daily_turns
        seats["triage"] = seat
    try:
        updated = config.normalize({**loop, "triage": block, "tokens": tokens, "seats": seats})
        if not config.profile_exists(updated["triage"]["profile"]):
            raise config.ConfigError(f"no Hermes profile named {updated['triage']['profile']!r} "
                                     f"(looked in {config.profiles_root()})")
        credentials.verify_credentials(updated, roles={"read"})
        config.webhook_host(updated["host"], required=True)
        cli_common._verify_routes(updated, {"triage"})
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    for line in _triage_lines(updated):
        print(line)
    if args.dry_run:
        hook_note = ""
        if admin:
            try:
                existing = [h for h in cli_common._hook_listing(updated, admin, require_active=False)
                            if routes.route_name_of(h["config"]["url"]) == updated["triage"]["route"]]
            except config.ConfigError:
                existing = None
            if existing is None:
                hook_note = ", and reconcile the repo hook"
            elif existing:
                dest = routes.url_for(updated["triage"]["route"],
                                      config.webhook_host(updated.get("host"), required=True))
                kept, _rest = _keep_one(existing, dest)   # the same choice reconciliation makes
                state = "active" if kept.get("active") else "paused"
                hook_note = f", and keep hook {kept['id']} ({state}), repointing it if needed"
            else:
                hook_note = ", and create the repo hook (paused; next, arm)"
        print(f"  dry run — would write route {updated['triage']['route']} (issues) and its gate "
              "shim" + hook_note + "; nothing written")
        return 0
    name = updated["triage"]["route"]
    previous_route = routes.route(name)
    try:
        cli_common._write_config(updated)
        cli_common._install_routes(updated, roles=("triage",))
        route_intent.record_live(updated, [name])
    except Exception as exc:
        try:
            routes.restore_entries({name: previous_route})
            cli_common._restore_config(path, previous_config)
        except Exception as rollback_exc:
            print(f"ROLLBACK FAILED: {rollback_exc} — inspect {path} and route {name}")
            return 2
        print(f"triage install FAILED: {exc}; config and route restored")
        return 2
    print(f"  route written: {name}")
    shims_ok = cli_common._install_shims(updated)
    if admin:
        outcome: dict = {}
        rc = _ensure_hooks(updated, admin, dry_run=False, outcome=outcome)
        if rc:
            return rc
        kept = (outcome.get("kept") or {}).get("triage")
        if kept is not None:
            state = "active" if kept.get("active") else "paused"
            print(f"  hook {kept['id']} kept ({state})")
            if not kept.get("active"):
                print(f"  next: hermes dk arm --loop {loop['id']} --admin-token {admin} "
                      "(the hook is paused; arm turns every loop hook on)")
        else:
            print("  hook created (paused)")
            print(f"  next: hermes dk arm --loop {loop['id']} --admin-token {admin} "
                  "(arm turns every loop hook on)")
    else:
        print(f"  next: hermes dk apply --loop {loop['id']} --hooks --admin-token LOGIN "
              "creates the issues hook (paused), then `arm`")
    return 0 if shims_ok else 1


def cmd_fixer_push(args) -> int:
    """Change only this repository's unattended push permission by explicit operator action."""
    try:
        with config.push_policy_lock():
            return _cmd_fixer_push_locked(args)
    except (OSError, ValueError, config.ConfigError) as exc:
        print(f"fixer push policy update not confirmed: {exc}")
        return 2


def _cmd_fixer_push_locked(args) -> int:
    try:
        loop = config.load_id(args.loop)
        config.by_repo(loop['repo'])  # duplicate owners fail closed
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    if args.enable and not args.acknowledge_pr_race:
        print("refused: host-operator policy only (not verified GitHub owner/admin consent). "
              "Unattended fixer pushes have a residual PR-metadata/ref race: "
              "closing, drafting or retargeting a PR between the last check and Git's "
              "exact-SHA-lease push can still publish. Inspect the production boundary and "
              "pass --acknowledge-pr-race to opt in for this repository.")
        return 2
    if args.enable:
        try:
            busy = cli_common._busy_seats(loop, {"fixer"})
            from ..run_supervisor import Supervisor, ACTIVE
            ledger = config.host_path("ledger")
            if ledger.exists():
                with Supervisor(ledger)._connect() as con:
                    rows = con.execute(
                        'SELECT pr,state FROM runs WHERE repo=? AND seat=? '
                        'AND state IN (?,?,?,?) LIMIT 1',
                        (loop['repo'], 'fixer', *ACTIVE)).fetchall()
                busy.extend(f"fixer supervisor on PR {row['pr']} ({row['state']})"
                            for row in rows)
        except (OSError, ValueError) as exc:
            print(f"refused: cannot check active fixer runs: {exc}")
            return 2
        if busy:
            print("refused: fixer run in flight; wait for it to finish before arming pushes: "
                  + "; ".join(busy))
            return 2
    enabled = bool(args.enable)
    if config.unattended_fixer_push_enabled(loop) == enabled:
        print(f"[{loop['id']}] unattended fixer push already {'enabled' if enabled else 'disabled'}")
        return 0
    updated = config.normalize({**loop, "unattended_fixer_push": enabled})
    if args.dry_run:
        print(f"[{loop['id']}] dry run — would {'enable' if enabled else 'disable'} "
              "unattended fixer pushes; nothing written")
        return 0
    try:
        path = cli_common._write_config_locked(updated, policy_change=True)
        actual = config.load_id(loop["id"])
        if actual["repo"] != loop["repo"] or config.unattended_fixer_push_enabled(actual) != enabled:
            raise config.ConfigError("readback does not match the requested repository/policy")
    except (OSError, ValueError, config.ConfigError) as exc:
        print(f"fixer push policy update not confirmed: {exc}")
        return 2
    print(f"[{loop['id']}] {loop['repo']}: host-operator unattended fixer push "
          f"{'enabled (not GitHub owner consent; residual PR-metadata/ref race acknowledged)' if enabled else 'disabled'} "
          f"in {path}")
    if enabled:
        try:
            from .. import state as state_mod
            held = [key for key, entry in state_mod.state_for(actual).queue_items("fixer").items()
                    if config.is_fixer_push_hold(entry)]
        except Exception:
            held = []
        if held:
            print(f"  {len(held)} held verdict(s) ({', '.join(sorted(held))}) start on the next "
                  f"watchdog sweep, or now: `hermes dk drain --loop {loop['id']} "
                  "--seat fixer`")
    return 0


# -- the parser for this module's commands (#676 step 7) --------------------------------------


def add_set(sub) -> None:  # noqa: ANN001
    """The ``set`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    change = sub.add_parser("set", help="Change a loop's settings in place")
    change.add_argument("--loop", required=True,
                        help="loop id (its config file name; `list` shows them)")
    change.add_argument("--concurrency", type=int,
                        help="default PRs per seat at once (1 = serialized; above 1 needs a "
                             "clone, since each run gets its own)")
    change.add_argument("--reviewer-concurrency", type=int, default=None,
                        help="how many PRs the reviewer may work at once")
    change.add_argument("--fixer-concurrency", type=int, default=None,
                        help="how many PRs the fixer may work at once")
    change.add_argument("--cap", type=int, help="verdicts allowed before adjudication")
    change.add_argument("--clone", help="local clone the runs isolate from")
    change.add_argument("--base", help="base branch the loop watches")
    change.add_argument("--grace-min", type=int, help="quiet minutes before the watchdog speaks")
    change.add_argument("--marker-grace-min", type=int,
                        help="minutes an adjudication may sit claimed with no live run before "
                             "the watchdog reports it")
    change.add_argument("--ttl-min", type=int, help="how long a run may hold its slot")
    change.add_argument("--inflight-ttl-min", type=int,
                        help="minutes an in-flight mark blocks a second run at the same head")
    change.add_argument("--turn-budget", type=int,
                        help="seconds one isolated seat turn may run (loop default)")
    change.add_argument("--attribution", choices=("on", "off"), default=None,
                        help="sign what the loop posts ('Automated by Diaktoros'), "
                             "or stop")
    change.add_argument("--review-only-update", choices=("on", "off"), default=None,
                        help="push a clean merge of the base into a review-only author's PR "
                             "branch (same repository only), or stop; turning it on needs "
                             "--acknowledge-branch-push")
    change.add_argument("--acknowledge-branch-push", action="store_true",
                        help="accept that the host pushes to a branch the loop does not own; "
                             "required to turn --review-only-update on")
    change.add_argument("--review-after-ci", choices=("on", "off"), default=None,
                        help="start each review after the head's checks finish (up to an hour), "
                             "or start at once")
    change.add_argument("--fix-ci", choices=("on", "off"), default=None,
                        help="hand a failed required check on a fixer's PR to the fixer "
                             "(needs unattended fixer pushes), or stop")
    change.add_argument("--fixer-scope-lock", choices=("on", "off"), default=None,
                        help="hold a fix round that changes files its findings do not name "
                             "(other than their tests and docs) for a maintainer, or stop; "
                             "needs unattended fixer pushes")
    change.add_argument("--review-only-cap", default=None, metavar="N",
                        help="verdicts the reviewer gives one review-only PR before it waits "
                             "for `review --another-round`, 1-1000; 0 = the review cap")
    change.add_argument("--ci-fix-cap", default=None, metavar="N",
                        help="CI-fix turns per PR before a red head waits for you, "
                             "1-10; 0 = the default (3)")
    change.add_argument("--review-only-daily", default=None, metavar="N",
                        help="reviewer turns a day on review-only PRs, 1-1000; 0 = no cap")
    reviewed = change.add_mutually_exclusive_group()
    reviewed.add_argument("--review-only", action="append", default=None,
                          help="a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it, or comma-separated); replaces the list")
    reviewed.add_argument("--no-review-only", action="store_true",
                          help="clear the review-only list")
    required = change.add_mutually_exclusive_group()
    required.add_argument("--required-check", action="append", default=None,
                          help="a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates); replaces the list")
    required.add_argument("--no-required-checks", action="store_true",
                          help="clear the list: every check gates again")
    shadows = change.add_mutually_exclusive_group()
    shadows.add_argument("--shadow-profile", default=None,
                         help="the Hermes profile shadow reviews run as (#218): a second reviewer "
                              "model whose verdicts are recorded, never posted; 'off' turns them off")
    shadows.add_argument("--no-shadow", action="store_true",
                         help="turn shadow reviews off")
    change.add_argument("--shadow-every", type=int, default=None,
                        help="shadow one in this many reviewer turns, 1-1000 (needs a shadow profile)")
    change.add_argument("--shadow-escalate", choices=("on", "off"), default=None,
                        help="on: a PR is not ready to merge while its shadow review is running, or after it requested changes (#1004); needs --shadow-every 1")
    humans = change.add_mutually_exclusive_group()
    humans.add_argument("--human-path", action="append", default=None,
                        help="a glob pattern for paths only a human may approve (repeat it); replaces the list")
    humans.add_argument("--no-human-paths", action="store_true",
                        help="clear the list: no path is reserved for a human")
    generated = change.add_mutually_exclusive_group()
    generated.add_argument("--generated", action="append", default=None, metavar="PATH[,PATH]=COMMAND",
                           help="generated files the host regenerates when a PR's only merge conflicts are in them: PATH[,PATH...]=COMMAND, run in the sandbox (repeat it); replaces the list")
    generated.add_argument("--no-generated", action="store_true",
                           help="clear the list: the conflict shortcut is off")
    change.add_argument("--fixer-check", default=None, help="one command the fixer runs before every push or issue-fix PR, besides its touched tests (chain several with &&; '' for none)")
    change.add_argument("--reviewer-turn-budget", type=int, default=None,
                        help="the reviewer seat's own turn budget in seconds")
    change.add_argument("--fixer-turn-budget", type=int, default=None,
                        help="the fixer seat's own turn budget in seconds")
    change.add_argument("--reviewer-daily-turns", type=int, default=None,
                        help="most reviewer turns per day on this loop; later ones wait for "
                             "midnight (0 removes the cap)")
    change.add_argument("--fixer-daily-turns", type=int, default=None,
                        help="most fixer turns per day on this loop (0 removes the cap)")
    change.add_argument("--reviewer-daily-tokens", type=int, default=None,
                        help="most tokens (in + out) the reviewer may spend per day on this "
                             "loop; once spent, the next turn waits for midnight (0 removes it)")
    change.add_argument("--fixer-daily-tokens", type=int, default=None,
                        help="most tokens the fixer (and issue fixes) may spend per day on "
                             "this loop (0 removes it)")
    change.add_argument("--shadow-daily-tokens", type=int, default=None,
                        help="most tokens shadow reviews may spend per day on this loop; once "
                             "spent, the next waits for midnight (0 removes it)")
    change.add_argument("--shadow-daily-turns", type=int, default=None,
                        help="most shadow reviews per day on this loop (0 removes the cap)")
    change.add_argument("--shadow-turn-budget", type=int, default=None,
                        help="a shadow review's own turn budget in seconds (0 = the loop's)")
    change.add_argument("--shadow-max-steps", type=int, default=None,
                        help="agent steps one shadow review may take, 8-200 (0 = default 100)")
    change.add_argument("--reviewer-max-steps", type=int, default=None,
                        help="agent steps one reviewer turn may take, 8-200 (0 = default 60)")
    change.add_argument("--fixer-max-steps", type=int, default=None,
                        help="agent steps one fixer or issue-fix turn may take, 8-200 "
                             "(0 = default 80)")
    change.add_argument("--host", help="gateway webhook host")
    change.add_argument("--adjudicator-login", default=None,
                        help="optional fourth GitHub account the ruling is also posted as; "
                             "\"\" clears it (rulings go to the operator only)")
    change.add_argument("--adjudicator-profile", default=None,
                        help="turn adjudication on: the Hermes profile that rules when the "
                             "verdict cap is spent; creates the <id>-breach route and its shim")
    change.add_argument("--adjudicator-route", default=None,
                        help="with --adjudicator-profile: the route's name (default: <id>-breach)")
    change.add_argument("--adjudicator", choices=("off",), default=None,
                        help="turn adjudication off: removes the route and the block (breach "
                             "markers and rulings stay in the ledger)")
    change.add_argument("--read-token", default=None,
                        help="the login the gates read GitHub as — its own account, never a "
                             "seat or the adjudicator login (the four-identity rule); map a "
                             "new login with --token LOGIN=/path")
    change.add_argument("--token", action="append", default=[],
                        help="LOGIN=/path/to/pat for the --read-token or --adjudicator-login "
                             "login only (a path, never the token)")
    change.add_argument("--observer-route", help="route the observer feed delivers through")
    change.add_argument("--observer-profile", help="profile that owns the observer destination")
    change.add_argument("--observer-deliver",
                        help="where the gateway delivers the feed (telegram, discord, ...)")
    change.add_argument("--observer-events", default=None,
                        help="comma-separated transitions to send, from "
                             "opened,handoff,verdict,approved,escalation,ruling,stall,closed,"
                             "triaged,fixing,fixed,failed,held,conflict,ci_failed,updated,main_red,stale_approval,human_paths,ready_set "
                             "(blank = all)")
    change.add_argument("--observer-digest-min", type=int, default=None,
                        help="batch the feed into one message per N minutes (0 = per "
                             "transition)")
    change.add_argument("--observer-weekly-report", default=None,
                        help="the week's report once a week at a weekday and UTC time like "
                             "'mon 09:00' (blank = none)")
    change.add_argument("--observer-urgent-route", default=None,
                        help="route for urgent notices only (blank = one feed for everything)")
    change.add_argument("--observer-urgent-profile", default=None,
                        help="profile that owns the urgent destination (blank = the feed's)")
    change.add_argument("--observer-urgent-deliver", default=None,
                        help="where the gateway delivers urgent notices (blank = the feed's)")
    change.add_argument("--observer-mute", action="store_true",
                        help="stop the feed without forgetting it")
    change.add_argument("--observer-unmute", action="store_true", help="resume a muted feed")
    change.add_argument("--observer-disable", action="store_true",
                        help="drop this loop's observer config entirely")
    change.set_defaults(func=cmd_set)


def add_apply(sub) -> None:  # noqa: ANN001
    """The ``apply`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    apply_cmd = sub.add_parser("apply", help="Push the plugin settings onto a loop")
    apply_cmd.add_argument("--loop", required=True,
                           help="loop id (its config file name; `list` shows them)")
    apply_cmd.add_argument("--dry-run", action="store_true",
                           help="show the diff without writing it")
    apply_cmd.add_argument("--while-busy", action="store_true",
                           help="rebind a seat's profile/login even while a run is in flight "
                                "(that run keeps the identity it started with)")
    apply_cmd.add_argument("--admin-token", default="",
                           help="login whose token can write the repo's hooks, for the hook "
                                "moves and re-keys apply makes (default: the reader)")
    apply_cmd.add_argument("--recreate-routes", action="store_true",
                           help="write this loop's routes the registry lost, from the loop "
                                "config, with a new secret, and re-key the repo hooks that "
                                "point at them (when no intent record can restore them)")
    apply_cmd.add_argument("--hooks", action="store_true",
                           help="make every repo hook this loop's routes need (reviewer, "
                                "fixer, and triage's issues hook when triage is on) what "
                                "they need: create a missing one (paused until arm), repoint one at the "
                                "route's exact URL, add its gate's event (hook write access, "
                                "see --admin-token)")
    apply_cmd.add_argument("--watchdog-shim", action="store_true",
                           help="rewrite the cron shim the watchdog job runs, pinned to this "
                                "plugin's watchdog")
    apply_cmd.set_defaults(func=cmd_apply)


def add_settings(sub) -> None:  # noqa: ANN001
    """The ``settings`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    settings_cmd = sub.add_parser("settings",
                                  help="Show the plugin-level defaults a loop starts from")
    settings_cmd.set_defaults(func=cmd_settings)


def add_show(sub) -> None:  # noqa: ANN001
    """The ``show`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    show_cmd = sub.add_parser("show",
                              help="Every setting of one loop: value, source, meaning")
    show_cmd.add_argument("--loop", required=True,
                          help="loop id (its config file name; `list` shows them)")
    show_cmd.add_argument("--json", action="store_true", help="machine-readable output")
    show_cmd.set_defaults(func=cmd_show)


def add_triage(sub) -> None:  # noqa: ANN001
    """The ``triage`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    triage = sub.add_parser("triage", help="Issue triage for one loop: --enable, --disable, "
                                           "or show it (#213)")
    triage.add_argument("--loop", required=True,
                        help="loop id (its config file name; `list` shows them)")
    switch = triage.add_mutually_exclusive_group()
    switch.add_argument("--enable", action="store_true",
                        help="turn triage on (or change it): writes its route, shim and, with "
                             "--admin-token, its issues hook (paused until arm)")
    switch.add_argument("--disable", action="store_true",
                        help="turn triage off: removes its route, shim and (with --admin-token) hook")
    triage.add_argument("--triage-profile", dest="profile", default="",
                        help="Hermes profile whose model triages")
    triage.add_argument("--author", action="append", default=[],
                        help="GitHub login whose new issues are triaged (repeatable); "
                             "anyone else's are ignored")
    triage.add_argument("--labels", default="",
                        help="comma-separated labels triage may apply, e.g. "
                             "bug,feature,docs,question,P0,P1,P2,P3")
    triage.add_argument("--max-labels", type=int, default=None,
                        help="at most this many labels per issue (default 3)")
    triage.add_argument("--comment", choices=("on", "off"), default=None,
                        help="allow one short comment with the labels (default off)")
    triage.add_argument("--login", default="",
                        help="account that labels (default: the reviewer seat); needs "
                             "issues: write, never the reader")
    triage.add_argument("--token", action="append", default=[],
                        help="login=/path/to/pat for --login (or --admin-token), if not mapped")
    triage.add_argument("--fix-label", default=None,
                        help="a label a maintainer applies to hand an issue to the fixer "
                             "(#214; needs unattended fixer pushes on); '' turns it off")
    triage.add_argument("--maintainer", action="append", default=[],
                        help="login whose applying --fix-label counts (repeatable)")
    triage.add_argument("--daily-turns", type=int, default=None,
                        help="at most this many triage turns per day (0 removes the cap)")
    triage.add_argument("--fix-daily-turns", type=int, default=None,
                        help="at most this many issue-fix turns per day (0 = the default, "
                             f"{config.DEFAULT_FIX_DAILY_TURNS}); issue fixes are always "
                             "capped")
    triage.add_argument("--auto-fix-label", action="append", default=None,
                        help="a triage label that hands an issue to the fixer without a "
                             "maintainer (#232; repeatable; never P0-P2; '' clears the list)")
    triage.add_argument("--auto-fix-daily", type=int, default=None,
                        help="at most this many automatic issue fixes per day (0 = the "
                             f"default, {config.DEFAULT_AUTO_FIX_DAILY})")
    triage.add_argument("--admin-token", default="",
                        help="login whose token can create or delete repo hooks")
    triage.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
    triage.set_defaults(func=cmd_triage)


def add_fixer_push(sub) -> None:  # noqa: ANN001
    """The ``fixer-push`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    fixer_push = sub.add_parser("fixer-push", help="Explicit per-repository unattended fixer push policy")
    fixer_push.add_argument("--loop", required=True, help="exact loop id (never all loops)")
    direction = fixer_push.add_mutually_exclusive_group(required=True)
    direction.add_argument("--enable", action="store_true", help="opt this repository in")
    direction.add_argument("--disable", action="store_true", help="turn unattended pushes off")
    fixer_push.add_argument("--acknowledge-pr-race", action="store_true",
                            help="accept the residual non-atomic PR-metadata/ref race; required for --enable")
    fixer_push.add_argument("--dry-run", action="store_true", help="show action without writing")
    fixer_push.set_defaults(func=cmd_fixer_push)
