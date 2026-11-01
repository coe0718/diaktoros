"""What several command groups share (#676), so a command module never imports ``cli``.

The install plumbing (routes and their prompts, webhooks, the watchdog's cron job and shim) and
the loop-file write; a loop's token mapping and credential lines; a seat's turn-budget line.
Callers use it as ``cli_common.<name>``, never a copied name, so a test that patches one helper
here reaches every caller.
"""
from __future__ import annotations

import json
import pathlib
import shlex
import shutil
import time

from .. import (
    config,
    doctor,
    envnames,
    gate_shims,
    gh,
    github_app,
    prompts,
    route_intent,
    routes,
    util,
)
from ..doctor import hook_checks
from ..config import host_guards


# The Hermes plugin context register_cli was given: `migrate` writes settings through it (#425).
_CTX = None

# What the plugin's settings form held when this process started (Capabilities → Plugins in the
# desktop). Set by `register_cli`; read by `init`, `setup`, `apply`, `settings` and `show`. A
# form that silently renumbered a running loop would be a nasty thing to debug, so these are
# *defaults* and a push — never a subscription.
_SETTINGS: dict = {}

# The root ``hermes dk`` parser, kept by register_cli so ``setup`` runs the other verbs
# through the very parser (and settings defaults) the operator would.
_PARSER = None

BRANCH_PUSH_REFUSAL = (
    "refused: review_only_update lets the host push a merge commit to a branch the loop does not "
    "own (a review-only author's, same repository only, only a clean merge, with a lease on the "
    "head it read), as the account that makes host merge pushes, and needs unattended fixer "
    "pushes. If the author pushes meanwhile the lease fails and nothing is overwritten, but a "
    "published merge cannot be undone by the loop. Pass --acknowledge-branch-push to opt in.")

# The shim and job names a fresh install gets; an install not yet migrated keeps the old pair
# (``config.watchdog_shim`` / ``config.watchdog_job_name`` say which is live).
SHIM_NAME = "diaktoros-watchdog.py"

# One shared job runs the shim (issue #60): `hermes cron create --script` takes only a filename
# under ~/.hermes/scripts/ and no arguments, so a job cannot carry `--loop <id>`. A single job
# whose shim runs the watchdog with no `--loop` sweeps every loop exactly once per tick — N loops
# cost N sweeps, not N². The shim is shared by that one job and is removed only with the last loop.
SHARED_JOB_NAME = "diaktoros watchdog"

SHARED_JOB_NAMES = frozenset(config.WATCHDOG_JOBS.values())

SHIM = '''#!/usr/bin/env python3
"""Cron shim written by `hermes dk init`.

The scheduler runs scripts from ~/.hermes/scripts/, so this forwards to the plugin's own
watchdog and delivers its stdout. Keep the plugin as the single copy of the code.
"""
import pathlib
import subprocess
import sys

WATCHDOG = pathlib.Path("{watchdog}")
if not WATCHDOG.exists():
    print(f"diaktoros watchdog is missing: {{WATCHDOG}}")
    sys.exit(0)

proc = subprocess.run([sys.executable, str(WATCHDOG), *sys.argv[1:]], capture_output=True, text=True)
if proc.stdout.strip():
    print(proc.stdout.strip())
if proc.returncode != 0 and proc.stderr.strip():
    print(f"diaktoros watchdog failed: {{proc.stderr.strip()[:400]}}")
'''

# Which gate script each role's route must run. Ownership is checked against this before a route
# is written: the registry is shared with every other plugin on the host, and rebinding someone
# else's route to our profile would be a silent takeover of their webhook.
GATE_SCRIPT = route_intent.GATE_SCRIPT

# Each role's route prompt: together with the gate script, the proof that a route is ours.
_ROUTE_PROMPT = route_intent.ROUTE_PROMPT


def admin_unmapped(loop: dict, admin: str, *, also: set | None = None) -> str:
    """Why ``--admin-token LOGIN`` cannot act on this loop, or "" (#1196): the login needs a
    credential mapped here, a PAT (``tokens``) or a GitHub App's key (``apps``). ``also`` names
    logins this same command is about to map. The fix names both ways to map one."""
    from .. import gh
    if not admin or gh.credential_path(loop, admin) is not None:
        return ""
    if admin.lower() in {str(x).lower() for x in (also or set())}:
        return ""
    lid = shlex.quote(str(loop.get("id") or "<id>"))
    return (f"--admin-token {admin!r} has no credential mapped on this loop — map a PAT with "
            f"`hermes dk set --loop {lid} --token {shlex.quote(admin)}=/abs/path/to/pat`, or a "
            f"GitHub App with `hermes dk set --loop {lid} --app "
            f"{shlex.quote(admin)}=APP_ID:/abs/path/to/key.pem` (it needs "
            "hook write access: classic `repo` or `admin:repo_hook` for a PAT, Webhooks: Read and "
            "write for an App)")

# One matcher for every caller (doctor.split_route_hooks): arm and selftest --ping credit a hook
# only at the registry's route URL; uninstall and init's stale-hook guard (ownership) also count
# the URL the loop's config gives a route the registry no longer holds.
_hook_route_name = hook_checks.hook_route_name


def _routes_of(loop: dict) -> dict:
    """role → route name, from the loop's own config: the seats own the names, not this module.

    A hand-edited loop may route its reviewer anywhere; every path that fires, verifies or
    rebinds a route must read that answer rather than re-derive the convention.
    """
    return route_intent.routes_of(loop)


def _credential_lines(loop: dict) -> list[str]:
    """Where each seat's credential is *referenced*: a PAT file, or a GitHub App's id and key
    file (#1163). Never a value: it lives in a 0600 file read at use time, and this CLI has no
    business copying it into a terminal."""
    lines: list[str] = []
    seen: set[str] = set()
    for role in ("reviewer", "fixer"):
        login = config.seat_login(loop, role)
        if not login or login.lower() in seen:
            continue
        seen.add(login.lower())
        lines.append(f"{role} {login} → "
                     f"{_credential_label(loop, login) or '(no file mapped — falls back to read_token)'}")
    read_token = loop.get("read_token") or ""
    if read_token and read_token.lower() not in seen:
        lines.append(f"read {read_token} → {_credential_label(loop, read_token) or '(no file mapped)'}")
    adj = config.adjudicator_login(loop)
    if adj:
        lines.append(f"adjudicator {adj} → {_credential_label(loop, adj) or '(no file mapped)'}")
    return lines


def _app_ref(loop: dict, login: str) -> dict:
    """The ``apps`` entry for ``login`` (exact key first, then any case), or ``{}``."""
    found = util.login_entry(loop.get("apps"), login)
    return found if isinstance(found, dict) else {}


def _credential_file(loop: dict, login: str) -> str:
    """The file ``login``'s credential lives in: its PAT, or its App's private key; ``""``."""
    return _token_ref(loop, login) or str(_app_ref(loop, login).get("key_file") or "")


def _credential_label(loop: dict, login: str) -> str:
    """How a credential is shown: the PAT path, or ``App <id>, key <path>``. Never a value."""
    token = _token_ref(loop, login)
    if token:
        return token
    app = _app_ref(loop, login)
    return f"App {app.get('app_id')}, key {app.get('key_file')}" if app else ""


def _split_token_pair(pair: str, flag: str = "--token") -> tuple[str, str] | None:
    """Parse one ``login=/path/to/pat`` pair: ``(login, path)`` with the login stripped, or ``None``
    after printing the refusal (no ``=``, or an empty login that would write a ``tokens[""]``
    entry nothing can use). ``flag`` names the option in the refusal: `init`, `set` and `triage`
    take ``--token``; `identity` passes ``--pat``."""
    if "=" not in pair:
        print(f"{flag} expects login=/path/to/pat, got {pair!r}")
        return None
    login, path = pair.split("=", 1)
    login = login.strip()
    if not login:
        print(f"refused: {flag} expects login=/path/to/pat, got {pair!r} with an empty login")
        return None
    return login, path


def _split_app_pair(pair: str) -> tuple[str, dict] | None:
    """Parse one ``--app LOGIN=APP_ID:/abs/key.pem`` (#1163): ``(login, {"app_id", "key_file"})``,
    or ``None`` after printing the refusal. The key file must pass the same checks as a token file
    (absolute, yours, mode 0600); it is never opened here."""
    from ..config import credentials
    usage = "--app expects <app-slug>[bot]=APP_ID:/abs/path/to/key.pem"
    if "=" not in pair or ":" not in pair.split("=", 1)[1]:
        print(f"{usage}, got {pair!r}")
        return None
    login, rest = pair.split("=", 1)
    login = login.strip()
    app_id, key = rest.split(":", 1)
    if not config.APP_LOGIN.match(login.lower()):
        print(f"refused: {usage}: {login!r} is not an App's bot login")
        return None
    if not app_id.strip().isdigit() or int(app_id) <= 0:
        print(f"refused: {usage}: the App id {app_id!r} is not a number")
        return None
    try:
        credentials.check_token_file(key, f"--app {login} key")
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return None
    return login, {"app_id": int(app_id), "key_file": str(pathlib.Path(key.strip()).expanduser())}


def _token_ref(loop: dict, login: str) -> str:
    """The token *path* mapped for ``login`` (exact key first, then any case), or ``""``."""
    return str(util.login_entry(loop.get("tokens"), login) or "")


def _budget_line(loop: dict) -> str:
    """Each seat's turn budget, the way status/doctor/set print it."""
    seats = ["reviewer", "fixer"] + (["adjudicator"] if (loop.get("adjudicator") or {}).get("route")
                                     else [])
    return " · ".join(f"{seat} {config.turn_budget(loop, seat)}s" for seat in seats)


def _legacy_job_name(loop: dict) -> str:
    """The pre-#60 per-loop job name, recognised only to migrate it away."""
    return f"review loop watchdog ({loop['id']})"


def routes_for(loop: dict) -> dict:
    """The conventional route names for a loop, and the names ``init`` writes."""
    return {"reviewer": f"{loop['id']}-review", "fixer": f"{loop['id']}-fix",
            "observer": f"{loop['id']}-observe",
            "adjudicator": f"{loop['id']}-breach", "triage": f"{loop['id']}-triage"}


def _verify_routes(loop: dict, roles) -> None:
    """Refuse to write a route that is not this loop's to write.

    Three rails: no other loop may already claim the name, no two roles of this loop may share one
    route (one route wakes one seat), and an installed route must have this role's exact gate and
    prompt. All three fail *before* anything is written, because a route is the one
    artifact here that another plugin could own.
    """
    mine = _routes_of(loop)
    route_roles = (*config.ROUTED_ROLES, "observer", "observer_urgent", "app")
    wanted = [role for role in route_roles if role in set(roles) and role in mine]
    names = [mine[role] for role in route_roles if role in mine]
    shared = sorted({name for name in names if names.count(name) > 1})
    if shared:
        raise config.ConfigError(
            f"{loop['id']}: {', '.join(shared)} is routed to more than one seat — one route wakes "
            "one seat, or two profiles fight over the same webhook")
    try:
        other_loops = config.all_loops()
    except config.ConfigError as exc:
        raise config.ConfigError(f"cannot check route ownership: {exc}") from exc
    registry = routes.all_routes()
    for role in wanted:
        name = mine[role]
        # "Itself" is the loop file being rewritten — same id *and* same repo. A loop that merely
        # shares the id is a different loop, and `init` writing its own file would take the routes
        # (and the repo) out from under it.
        owners = [other["id"] for other in other_loops
                  if not (other["id"] == loop["id"] and other.get("repo") == loop.get("repo"))
                  and name in set(_routes_of(other).values())
                  # An App's route is shared by every loop it delivers for (#1239): another
                  # loop's claim through its own app role is the same App's, not a collision.
                  and not (role == "app" and _routes_of(other).get("app") == name)]
        if owners:
            raise config.ConfigError(
                f"route {name!r} already belongs to loop {', '.join(sorted(owners))} — give this "
                "loop its own route name (the registry is shared by every plugin)")
        if role == "app":
            # One App webhook, one URL: every loop sharing its route must name the same gateway.
            hosts = sorted({str(other.get("host") or "").removesuffix("/") for other in other_loops
                            if other["id"] != loop["id"] and _routes_of(other).get("app") == name}
                           - {str(loop.get("host") or "").removesuffix("/")} - {""})
            if hosts:
                raise config.ConfigError(
                    f"route {name!r} is {loop.get('ingress_app')}'s, shared with loops on "
                    f"{', '.join(hosts)}: an App's webhook has one URL, so every loop it delivers "
                    "for must use the same --host")
        if name not in registry:
            continue                  # not installed yet: nothing of someone else's to collide with
        entry = registry[name]
        if not isinstance(entry, dict):
            raise config.ConfigError(f"route {name!r} exists but its ownership cannot be verified "
                                     "— pick another route name")
        script = entry.get("script")
        if script != GATE_SCRIPT[role] and script not in config.LEGACY_GATE_SCRIPTS.get(role, ()):
            raise config.ConfigError(
                f"route {name!r} runs {script!r}, not {GATE_SCRIPT[role]!r} — it belongs to "
                "something else; pick another route name")
        if entry.get("prompt") != _ROUTE_PROMPT[role]:
            raise config.ConfigError(
                f"route {name!r} does not have this {role} gate's prompt — ownership cannot be "
                "verified; pick another route name")


def _install_routes(loop: dict, roles=None) -> dict:
    """Write (or rewrite) the loop's routes; return only the roles actually written.

    Rewriting is idempotent and keeps the existing secret, and it is how a stale route gets fixed:
    the URL itself carries the seat's profile, so a seat that changed profile has a route that no
    longer points at the right one until this runs. Nothing else in the registry is touched —
    another loop's routes and their secrets survive this untouched.
    """
    names = _routes_of(loop)
    host = config.webhook_host(loop.get("host"), required=True)
    skill = loop.get("skill") or ""
    wanted = ((*config.ROUTED_ROLES, "observer", "observer_urgent", "app") if roles is None
              else tuple(roles))
    written: dict = {}
    for role in config.ROUTED_ROLES:
        if role not in wanted or role not in names:
            continue
        name = names[role]
        common = {"script": GATE_SCRIPT[role], "host": host,
                  "skills": [skill] if skill else []}
        if role == "reviewer":
            routes.new_route(name, profile=config.seat_profile(loop, "reviewer"),
                             prompt=prompts.REVIEWER, events=["pull_request"],
                             deliver="discord",
                             description=f"{loop['repo']} — wake the reviewer for a new or "
                                          "requested review", **common)
        elif role == "fixer":
            routes.new_route(name, profile=config.seat_profile(loop, "fixer"),
                             prompt=prompts.FIXER, events=["pull_request_review"],
                             deliver="discord",
                             description=f"{loop['repo']} — wake the fixer on a changes-requested "
                                          "verdict", **common)
        elif role == "triage":
            routes.new_route(name, profile=config.seat_profile(loop, "triage"),
                             prompt=prompts.TRIAGE, events=["issues"], deliver="discord",
                             description=f"{loop['repo']} — triage a new issue from an "
                                          "allowlisted author", **common)
        else:
            adjudicator = loop.get("adjudicator") or {}
            routes.new_route(name, profile=config.seat_profile(loop, "adjudicator"),
                             prompt=prompts.ADJUDICATOR, events=["pull_request"],
                             deliver=adjudicator.get("deliver", "telegram"),
                             description=f"{loop['repo']} — adjudicate a loop that spent its "
                                          "budget", **common)
        written[role] = name
    if "app" in wanted and "app" in names:
        # #1239: one route per App, shared by every loop it delivers for, so its profile is the
        # host's default rather than any one loop's seat (loops must not rewrite it to their own).
        routes.new_route(names["app"], profile="default", prompt=prompts.APP_EVENTS,
                         events=["pull_request", "pull_request_review", "issues", "ping"],
                         script=GATE_SCRIPT["app"], deliver="discord", host=host,
                         description=f"GitHub App {loop['ingress_app']} — events for every loop "
                                     "it delivers for")
        written["app"] = names["app"]
    if "observer" in wanted and "observer" in names:
        observer_cfg = loop["observer"]
        name = names["observer"]
        routes.new_route(name, profile=config.seat_profile(loop, "observer"),
                         prompt=prompts.OBSERVER, events=["pull_request"],
                         script="observe.py", deliver=observer_cfg.get("deliver", "telegram"),
                         deliver_only=True, host=host,
                         description=f"{loop['repo']} — read-only observer feed: one short "
                                     "notice per loop transition")
        written["observer"] = name
    if "observer_urgent" in wanted and "observer_urgent" in names:
        observer_cfg = loop["observer"]
        name = names["observer_urgent"]
        routes.new_route(name, profile=config.seat_profile(loop, "observer_urgent"),
                         prompt=prompts.OBSERVER, events=["pull_request"],
                         script="observe.py",
                         deliver=observer_cfg.get("urgent_deliver") or observer_cfg.get(
                             "deliver", "telegram"),
                         deliver_only=True, host=host,
                         description=f"{loop['repo']} — read-only observer feed: urgent "
                                     "notices only")
        written["observer_urgent"] = name
    return written


def _busy_seats(loop: dict, roles: set[str]) -> list[str]:
    """Seats with a run in flight right now — the ones an identity change must not surprise."""
    from .. import state as state_mod

    st = state_mod.state_for(loop)
    lines = []
    for seat in ("reviewer", "fixer"):
        if seat not in roles:
            continue
        live = st.active(seat)
        if live:
            held = ", ".join(f"{key} ({int((time.time() - entry.get('at', time.time())) / 60)}m)"
                             for key, entry in sorted(live.items()))
            lines.append(f"{seat} is in flight on {held}")
    return lines


def _observer_check(loop: dict) -> None:
    """Refuse an observer destination the gateway could not deliver without waking an agent.

    ``deliver_only`` is the gateway's no-agent mode and it rejects a ``log`` target outright: a
    route configured that way does not merely fail to deliver a notice, it stops the gateway from
    starting. So the mistake is caught here, before anything is written, rather than at the
    gateway's next restart.
    """
    observer_cfg = loop.get("observer") or {}
    if observer_cfg.get("route") and (observer_cfg.get("deliver") or "log") == "log":
        raise config.ConfigError(
            "observer.deliver must be a real destination (telegram, discord, ...) — an observer "
            "route never wakes an agent, and the gateway refuses a deliver_only file target")


def _on_off(args, name: str, default):
    """An ``on|off`` flag as a bool; ``default`` when the flag was not given."""
    value = getattr(args, name, None)
    return default if value is None else value == "on"


def _attribution_arg(args, default):
    """``--attribution on|off`` as a bool; ``default`` when the flag was not given (#197)."""
    value = getattr(args, "attribution", None)
    return default if value is None else value == "on"


def _install_hooks(loop: dict, token_login: str | None, active: bool = False,
                   seats=None) -> list[str]:
    """Create the loop's repo hooks via the API (every hooked role, or only ``seats``). Needs hook
    write access on the repo: classic ``repo``, or the narrower ``admin:repo_hook``."""
    names = _routes_of(loop)
    host = config.webhook_host(loop.get("host"), required=True)
    # Validate every destination and secret before creating any external hook.
    hooks = []
    for seat in config.hook_roles(loop):
        event = config.HOOK_EVENT[seat]
        if seats is not None and seat not in seats:
            continue
        route_name = names.get(seat, "")
        url = routes.url_for(route_name, host)
        secret = (routes.route(route_name) or {}).get("secret", "")
        if not url or not secret:
            raise config.ConfigError(f"route {route_name!r} needs a valid webhook URL and secret before installing hooks")
        hooks.append((event, url, secret))
    baseline = _hook_listing(loop, token_login, require_active=False)
    created = []
    try:
        for event, url, secret in hooks:
            # Created paused unless the operator asked for --arm: a loop goes live with `arm`,
            # after `doctor` and `selftest` pass, never as a side effect of writing its config.
            body = {"name": "web", "active": active, "events": [event],
                    "config": {"url": url, "content_type": "json", "secret": secret,
                               "insecure_ssl": "0"}}
            result = gh.api(loop, f"/repos/{loop['repo']}/hooks", method="POST", body=body,
                            login=token_login or loop.get("read_token"))
            if not isinstance(result, dict) or not isinstance(result.get("id"), int):
                raise config.ConfigError(f"hook creation not confirmed for {url}: "
                                         f"{hook_write_need(loop, token_login)}")
            created.append(result["id"])
        state = "armed" if active else "paused"
        return [f"hook {id} → {url} ({state})" for id, (_, url, _) in zip(created, hooks)]
    except Exception as exc:
        failures = []
        # A lost POST response may still have created a hook: compare with the baseline.
        try:
            current = _hook_listing(loop, token_login, require_active=False)
            target_urls = {url for _, url, _ in hooks}
            created = list(set(created) | {h["id"] for h in current
                           if h["id"] not in {b["id"] for b in baseline}
                           and h["config"]["url"] in target_urls})
        except Exception as read_exc:
            failures.append(f"cannot identify newly created hooks: {read_exc}")
        for hook_id in created:
            try:
                gh.api(loop, f"/repos/{loop['repo']}/hooks/{hook_id}", method="DELETE",
                       login=token_login or loop.get("read_token"))
                if any(h["id"] == hook_id for h in _hook_listing(loop, token_login, require_active=False)):
                    raise config.ConfigError("still present after DELETE")
            except Exception as rollback_exc:
                failures.append(f"hook {hook_id}: {rollback_exc}")
        raise config.ConfigError(f"hook install failed: {exc}; " +
                                 ("ROLLBACK FAILED: " + "; ".join(failures) if failures
                                  else "created hooks removed")) from exc


class HookAccessError(config.ConfigError):
    """GitHub did not let this token read or write the repo's hooks. Only this cause is fixed by
    a different token; every other hook error names its own remedy."""


def _hook_fix(loop: dict, exc: Exception, token_login: str | None = None) -> str:
    """The remedy for a failed hook step, matched to its cause."""
    if isinstance(exc, HookAccessError):
        return f"re-run with --admin-token <login> ({hook_write_need(loop, token_login)})"
    return f"`hermes dk doctor --loop {loop['id']}` names what to repair first"


def _hook_listing(loop: dict, token_login: str | None = None, *,
                  require_active: bool = True) -> list[dict]:
    hooks = gh.api(loop, f"/repos/{loop['repo']}/hooks?per_page=100",
                   login=token_login or loop.get("read_token"))
    # ``active`` is part of a valid entry where a choice depends on it (which of two hooks for one
    # route is kept). `arm` asks for ``require_active=False``: it flips and reads back a hook with
    # no real bool rather than trusting — or refusing — the listing's word for its state (#55).
    if not isinstance(hooks, list) or len(hooks) >= 100 or any(
        not isinstance(h, dict) or not isinstance(h.get("id"), int) or
        (require_active and not isinstance(h.get("active"), bool)) or
        not isinstance(h.get("config"), dict) or
        not isinstance(h["config"].get("url"), str) for h in hooks
    ):
        raise HookAccessError("cannot read a complete, valid repo hook listing; no changes made")
    return hooks


def _app_hook_need(login: str, repo) -> str:
    return f"the App key for {login!r} needs hook write access on {repo} (Webhooks: Read and write)"


def hook_write_need_any(loop: dict) -> str:
    """The hook-write remedy where the admin login is not known yet (doctor's ``--admin-token
    <login>``): the PAT wording and the App alternative, since either kind can be the admin (#1569)."""
    return (f"{hook_write_need(loop, '<login>')}; if that login is a GitHub App, its key needs hook "
            f"write access on {loop.get('repo')} instead (Webhooks: Read and write), the App installed "
            "on the repo and mapped with `--app <login>=APP_ID:/abs/path/to/key.pem`")


def hook_write_need(loop: dict, token_login: str | None) -> str:
    """Which login's file creates, arms and pauses this loop's hooks, and what it must carry.

    ``init --hooks`` and ``arm`` act as ``--admin-token``'s login, else as the reader. The reader
    is the one role that can hold a read-only token — and on a user-owned repo it is usually the
    owner, the only account that can manage hooks at all — so say which case this is.
    """
    reader = str(loop.get("read_token") or "")
    login = token_login or reader
    if github_app.is_app(loop, login):
        # An App has a key and a permission, not a token file and PAT scopes (#1276); the
        # wording agrees with admin_unmapped.
        need = _app_hook_need(login, loop.get("repo"))
        return need + (" — that is the reader's App" if login.lower() == reader.lower() else
                       " — check that App's permission and that it is installed on the repo")
    need = (f"the token for {login!r} needs hook write access on {loop.get('repo')} (classic `repo` or "
            "`admin:repo_hook`, or fine-grained `repository_hooks: write`)")
    if login and login.lower() == reader.lower():
        need += (" — that is the reader's file. On a user-owned repo only the owner can manage "
                 "hooks, so if the reader is the owner give that file hook write; otherwise pass "
                 "--admin-token <owner login> (a login mapped with its own --token at init)")
    else:
        need += " — check that login's token file"
    return need


def _hook_config(url: str, insecure_ssl=None) -> dict:
    """The complete config a loop hook for ``url`` must carry. Always sent whole: GitHub may treat
    a PATCHed ``config`` as a replacement, and a url-only body would then drop the secret, leaving
    a hook whose deliveries the route rejects. The secret is the route's own, from the registry;
    it is never printed or logged. ``insecure_ssl`` is the hook's own (the operator's TLS choice,
    read from the hook listing); ``"0"`` — verify TLS — only when the hook has none."""
    name = routes.route_name_of(url)
    secret = (routes.route(name) or {}).get("secret") or ""
    if not name or not secret:
        raise config.ConfigError(f"route {name or url!r} has no secret in the registry; "
                                 "the hook was not changed")
    ssl = "0" if insecure_ssl in (None, "") else str(insecure_ssl)
    return {"url": url, "content_type": "json", "insecure_ssl": ssl, "secret": secret}


def _patch_hook_url(loop: dict, hook_id: int, url: str, login: str | None = None,
                    require_secret: bool = False, insecure_ssl=None) -> None:
    path = f"/repos/{loop['repo']}/hooks/{hook_id}"
    login = login or loop.get("read_token")
    result = gh.api(loop, path, method="PATCH", body={"config": _hook_config(url, insecure_ssl)}, login=login)
    # A lost response is ambiguous. Always read back and roll back if it does not agree.
    actual = gh.api(loop, path, login=login)
    got = (actual.get("config") or {}) if isinstance(actual, dict) else {}
    if not isinstance(result, dict) or not isinstance(actual, dict) or got.get("url") != url \
            or (require_secret and not got.get("secret")):
        raise HookAccessError(f"hook {hook_id} update not confirmed as {url!r} — "
                              f"{hook_write_need(loop, login)}")


def _hook_route_names(loop: dict) -> set[str]:
    """The route names a repo hook of this loop posts to (both seats, and triage when on). A loop
    on its App's webhook has none on purpose, but one left from before the switch still posts to
    a seat's route (#1239): those names, so uninstall deletes such a hook and init refuses it."""
    roles = (("reviewer", "fixer", "triage") if config.app_ingress(loop)
             else config.hook_roles(loop))
    return {name for role, name in _routes_of(loop).items() if role in roles and name}


def _classify_hooks(loop: dict, login: str | None) -> tuple[list[dict] | None, list[dict], str]:
    """``(own, foreign, error)`` for the hooks posting to this loop's route names.

    *Own* hooks post to exactly one of the routes' URLs (``doctor.seat_hook_url``: the gateway
    binds a route to its profile by URL and answers any other profile's URL 404). *Foreign* ones
    post to the same route name at another origin, profile or path — another install, an old
    gateway — and are only ever reported, never deleted or counted as a collision. Every page is read; a partial or malformed listing is
    ``(None, [], reason)``, never "no hooks".
    """
    listing, error = gh.hooks_read(loop, login or loop.get("read_token"))
    if error:
        return None, [], error
    if any(not isinstance(hook.get("id"), int) or not isinstance(hook.get("config"), dict)
           or not isinstance(hook["config"].get("url"), str) for hook in listing):
        return None, [], "invalid hook listing"
    try:
        own, foreign = hook_checks.split_route_hooks(loop, listing, _hook_route_names(loop),
                                                     ownership=True)
    except config.ConfigError as exc:
        return None, [], f"cannot resolve the loop's webhook host: {exc}"
    return own, foreign, ""


def _loop_hooks(loop: dict, login: str | None) -> tuple[list[dict] | None, str]:
    """This loop's own repo hooks (see ``_classify_hooks``)."""
    own, _, error = _classify_hooks(loop, login)
    return own, error


def _foreign_lines(loop: dict, foreign: list[dict]) -> list[str]:
    lines = []
    for hook in foreign:
        name = _hook_route_name(hook)
        why = hook_checks.hook_url_difference(str(hook["config"].get("url") or ""),
                                              (hook_checks.install_hook_urls(loop, name) or [""])[0])
        lines.append(f"hook {hook['id']} posts to route {name!r} at {why} — not this install's "
                     "hook (not the route's URL), left alone")
    return lines


def _hook_delete_commands(loop: dict, hook_ids) -> list[str]:
    """Pasteable ``gh`` commands that delete these hooks (a token with admin:repo_hook or repo)."""
    repo = loop["repo"]
    return [f"gh api -X DELETE {shlex.quote(f'repos/{repo}/hooks/{hook_id}')}"
            for hook_id in hook_ids]


def _hermes_bin() -> str | None:
    """The ``hermes`` executable the scheduler commands run. ``DIAKTOROS_HERMES`` names a
    stand-in (the test suite's fake), so no test ever drives the operator's real install."""
    found = envnames.get("HERMES") or shutil.which("hermes")
    # Under the test guard (#101), never the operator's real hermes — for cron create and remove.
    return host_guards.guard_real_hermes(found) if found else None


def _cron_jobs(loop: dict) -> tuple[list[dict] | None, str]:
    """The scheduler watchdog job(s) for this install, read from the job store.

    The shared job (``SHARED_JOB_NAME``) is the one ``init`` creates; a legacy per-loop job
    (pre-#60) is returned too so it can be recognised and migrated.
    """
    path = doctor.cron_store()
    if not path.exists():
        return [], ""
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return None, f"{path} is not readable JSON ({exc})"
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return None, f"{path} has no job list"
    wanted = {*SHARED_JOB_NAMES, _legacy_job_name(loop)}
    return [job for job in jobs if isinstance(job, dict)
            and str(job.get("name") or "").strip() in wanted], ""


def _write_watchdog_shim() -> pathlib.Path:
    """The cron shim the watchdog job runs by name, pinned to this plugin's watchdog."""
    shim = config.watchdog_shim()
    shim.parent.mkdir(parents=True, exist_ok=True)
    watchdog = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "watchdog.py"
    shim.write_text(SHIM.format(watchdog=watchdog))
    shim.chmod(0o755)
    return shim


def _install_shims(loop: dict, report: bool = True, pairs=None) -> bool:
    """Write the loop's gate shims where the gateway resolves route scripts (issue #105)."""
    try:
        for line in gate_shims.install(loop, report=report, pairs=pairs):
            print(f"  {line}")
        return True
    except (OSError, config.ConfigError) as exc:
        print(f"gate shim install FAILED: {exc}")
        print(f"  fix it, then: hermes dk apply --loop {loop['id']}")
        return False


def _write_config(loop: dict, *, policy_change: bool = False) -> pathlib.Path:
    with config.push_policy_lock():
        return _write_config_locked(loop, policy_change=policy_change)


def _write_config_locked(loop: dict, *, policy_change: bool = False) -> pathlib.Path:
    directory = config.config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{loop['id']}.json"
    if path.is_symlink():
        raise config.ConfigError('symlinked loop config refused')
    if path.exists() and not policy_change:
        # Set/apply snapshots never own this switch. Re-read under the same lock
        # used by explicit enable/disable and by the broker's ref operation.
        try:
            current = config.load_id(loop['id'])
        except config.ConfigError:
            # `set --read-token` repairing a file whose only defect is its missing reader.
            current = config.load_id_for_reader_repair(loop['id'],
                                                       str(loop.get('read_token') or ''))
            if current is None:
                raise
        if current['repo'] != loop['repo']:
            raise config.ConfigError('repository changed during config update')
        loop = {**loop, 'unattended_fixer_push': current['unattended_fixer_push']}
    payload = json.dumps({k: v for k, v in loop.items() if v not in ({}, [], "")},
                         indent=2, sort_keys=True)
    util.atomic_write(path, payload)
    return path


def _restore_config(path: pathlib.Path, data: bytes) -> None:
    """Publish a previous snapshot without exposing partially restored JSON."""
    with config.push_policy_lock():
        if path.is_symlink():
            raise config.ConfigError('symlinked loop config refused')
        if path.exists():
            current = config.load_id(path.stem)
            snapshot = json.loads(data)
            if snapshot.get('repo', '').lower() != current['repo']:
                raise config.ConfigError('repository changed during config rollback')
            if snapshot.get('unattended_fixer_push', False) != current['unattended_fixer_push']:
                snapshot['unattended_fixer_push'] = current['unattended_fixer_push']
                data = json.dumps(snapshot, indent=2, sort_keys=True).encode()
        _restore_config_locked(path, data)


def _restore_config_locked(path: pathlib.Path, data: bytes) -> None:
    util.atomic_write(path, data)


def _parallel_lines(loop: dict) -> list[str]:
    """What ``init`` and ``set`` say about capacity: a note per pinned seat, then the effective
    limits. A loop default that a seat overrides is exactly the setting someone changes twice and
    wonders why nothing moved, so the override is said out loud."""
    lines = [f"note: {seat} has its own concurrency ({loop['seats'][seat]['concurrency']}) — "
             "the loop default does not apply to it"
             for seat in ("reviewer", "fixer")
             if (loop["seats"].get(seat) or {}).get("concurrency") is not None]
    lines.append("parallel now: " + " · ".join(
        f"{seat} {config.seat_concurrency(loop, seat)}" for seat in ("reviewer", "fixer"))
        + "   (1 = serialized; everything above the limit queues)")
    return lines


def _ping_loop_hooks(loop: dict, login: str | None) -> bool:
    """After arming: ping each of the loop's hooks and report how the gateway answered.

    An active hook whose secret the route does not hold looks armed and wakes nothing; this is
    the moment to find out. False only on a ping the gateway rejected or one that could not be
    sent — no delivery seen within the bounded wait is a warning, not a verdict.
    """
    from .. import hook_ping
    hooks, error = _loop_hooks(loop, login)
    if hooks is None:
        print(f"[{loop['id']}] ⚠️ hooks not pinged: cannot read the repo's hooks ({error})")
        return True
    ok = True
    for hook in sorted(hooks, key=lambda item: item["id"]):
        status, line = hook_ping.ping(loop, hook["id"], login)
        for part in line.split("\n"):
            print(f"[{loop['id']}] {part}")
        if status in (hook_ping.REJECTED, hook_ping.ERROR) and not line.startswith("⚠️"):
            ok = False
    return ok
