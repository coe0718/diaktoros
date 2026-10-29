"""The route and hook reconciliation a loop change needs (#1349, split from ``settings``):
which routes and hooks a change moves, rebinds, recreates or leaves, used by ``apply`` and
``triage`` (and ``show``'s route summary). Called as ``route_sync.<name>`` (one binding).
"""
from __future__ import annotations

from .. import (
    config,
    gate_shims,
    gh,
    route_intent,
    routes,
)
from . import common as cli_common
from ..config import credentials
from .common import _ROUTE_PROMPT, GATE_SCRIPT


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
        before, after = cli_common._credential_label(was, login), cli_common._credential_label(now, login)
        if before != after:
            label = "credential" if cli_common._app_ref(was, login) or cli_common._app_ref(now, login) else "token file"
            diffs.append((f"{role} {label} ({login})", before or "(none)", after or "(none)"))
            touched.add(role)
    # The adjudicator's comment identity has no run of its own to strand: it is reported, and
    # validated, but it does not rebind a route.
    login_before, login_after = config.adjudicator_login(was), config.adjudicator_login(now)
    if login_before != login_after:
        diffs.append(("adjudicator login", login_before or "(none)", login_after or "(none)"))
    if login_after:
        before, after = cli_common._credential_label(was, login_after), cli_common._credential_label(now, login_after)
        if before != after:
            label = "credential" if cli_common._app_ref(was, login_after) or cli_common._app_ref(now, login_after) else "token file"
            diffs.append((f"adjudicator {label} ({login_after})", before or "(none)",
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
    """``apply --hooks``: make each of this loop's repo hooks what its routes need (reviewer and
    fixer, plus triage when it is enabled; none under a GitHub App's ingress, see
    ``config.hook_roles``), the way ``init --hooks`` would have for a new loop (``init``
    refuses an existing one).

    Per hook role, hooks are matched by route name and one is kept by #106's ``_keep_one`` (active
    first). A role with none gets one created, paused until ``arm``. The kept hook is repointed at
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
