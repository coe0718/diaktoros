"""``hermes dk triage``: issue triage and issue fixes for a loop (#1349, split from
``settings``).
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
    route_intent,
    routes,
)
from . import common as cli_common
from . import route_sync
from ..config import credentials


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
            ("  maintainers: " + ", ".join(triage["maintainers"])
             + " (their comments reach both seats; their re-request starts a fresh review)")
            if triage.get("maintainers") else
            "  maintainers: none — no one's comments reach the seats, and no re-request starts a "
            "fresh review (--maintainer LOGIN)",
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
    # `--fix-label ''` is a change (it turns issue fixes off, #1217): its default is None, so
    # an empty string can only have been given on purpose. Every other "" or [] is no change.
    return ["--" + name.replace("_", "-") for name in _TRIAGE_CHANGE_FLAGS
            if getattr(args, name, None) not in (None, "", [])
            or (name == "fix_label" and getattr(args, name, None) == "")]


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
    refusal = cli_common.admin_unmapped(loop, admin, also={
        str(pair.split("=", 1)[0]).strip() for pair in args.token or []})
    if refusal:
        print(f"refused: {refusal}")
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
    # The maintainers and the issue-fix daily cap stand on their own (#1217): turning issue
    # fixes off (--fix-label '') keeps them. --maintainer '' clears the list.
    wanted = [m for m in args.maintainer or [] if m.strip()]
    maintainers = (wanted if wanted else [] if args.maintainer else current.get("maintainers") or [])
    if maintainers:
        block["maintainers"] = maintainers
    # The issue-fix daily cap (#247); omitted keeps the current one, 0 the default.
    fix_cap = (current.get("fix_daily_turns") if args.fix_daily_turns is None
               else args.fix_daily_turns or None)
    if fix_cap:
        block["fix_daily_turns"] = fix_cap
    fix_label = current.get("fix_label") if args.fix_label is None else args.fix_label
    if fix_label:
        block["fix_label"] = fix_label
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
        split = cli_common._split_token_pair(pair)
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
                kept, _rest = route_sync._keep_one(existing, dest)   # the same choice reconciliation makes
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
        rc = route_sync._ensure_hooks(updated, admin, dry_run=False, outcome=outcome)
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
                        help="a maintainer's login (repeatable; '' clears the list): their "
                             "comments reach both seats, their re-request starts a fresh review, "
                             "and their applying --fix-label hands an issue to the fixer. Kept "
                             "when --fix-label '' turns issue fixes off")
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
