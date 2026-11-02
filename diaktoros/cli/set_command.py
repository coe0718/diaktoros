"""``hermes dk set``: change one loop's settings (#1349, split from ``settings``). Its
argument parser is ``set_parser``.
"""
from __future__ import annotations

import pathlib

from .. import (
    config,
    gate_shims,
    observer,
    prompts,
    route_intent,
    routes,
    state as state_mod,
)
from . import common as cli_common
from . import set_adjudication
from . import triage_command
from ..config import credentials
from ..config import lists
from .common import BRANCH_PUSH_REFUSAL


class _Refused(Exception):
    """A ``set`` step refused and has said why; ``cmd_set`` returns 2 (#1349)."""


def _load_loop(args) -> dict:
    """The loop ``set`` changes, or the one reader repair a refused file allows."""
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
            raise _Refused
        print(f"repairing {args.loop}: it has no read_token; setting the reader named by --read-token")
    return loop


def _host_arg(args) -> str | None:
    """``--host``, stripped and checked: a blank one is refused, not ignored."""
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
            raise _Refused
    return host


def _flag_changes(args, loop: dict, host: str | None) -> dict:
    """The top-level scalar settings that differ from the loop's."""
    wanted = {"concurrency": args.concurrency, "cap": args.cap, "base": args.base,
              "clone": args.clone, "grace_min": args.grace_min,
              "marker_grace_min": args.marker_grace_min, "ttl_min": args.ttl_min,
              "inflight_ttl_min": args.inflight_ttl_min, "host": host,
              "turn_budget_s": getattr(args, "turn_budget", None),
              "attribution": cli_common._attribution_arg(args, None),
              "review_after_ci": cli_common._on_off(args, "review_after_ci", None),
              "fix_ci": cli_common._on_off(args, "fix_ci", None),
              "fixer_scope_lock": cli_common._on_off(args, "fixer_scope_lock", None),
              "adjudicator_approves": cli_common._on_off(args, "adjudicator_approves", None),
              "review_only_update": cli_common._on_off(args, "review_only_update", None)}
    if (wanted["review_only_update"] and not loop.get("review_only_update")
            and not getattr(args, "acknowledge_branch_push", False)):
        print(BRANCH_PUSH_REFUSAL)
        raise _Refused
    changes = {k: v for k, v in wanted.items()
               if v is not None and v != "" and v != loop.get(k)}
    return changes


def _list_changes(args, loop: dict, changes: dict) -> None:
    """The list and block settings: each flag, or its ``--no-`` form, replaces the whole value."""
    if getattr(args, "required_check", None) is not None or getattr(args, "no_required_checks",
                                                                     False):
        try:
            names = lists.check_required_checks(
                [] if args.no_required_checks else args.required_check, "--required-check")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            raise _Refused
        if names != (loop.get("required_checks") or []):
            changes["required_checks"] = names
    if getattr(args, "human_path", None) is not None or getattr(args, "no_human_paths", False):
        try:
            names = lists.check_human_paths(
                [] if args.no_human_paths else args.human_path, "--human-path")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            raise _Refused
        if names != (loop.get("human_paths") or []):
            changes["human_paths"] = names
    if getattr(args, "ci_covers", None) is not None or getattr(args, "no_ci_coverage", False):
        try:
            covers = lists.parse_ci_coverage(
                [] if args.no_ci_coverage else args.ci_covers, "--ci-covers")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            raise _Refused
        if covers != (loop.get("ci_coverage") or {}):
            changes["ci_coverage"] = covers
    if getattr(args, "generated", None) is not None or getattr(args, "no_generated", False):
        try:
            names = lists.parse_generated(
                [] if args.no_generated else args.generated, "--generated")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            raise _Refused
        if names != (loop.get("generated") or []):
            changes["generated"] = names
    if getattr(args, "no_shadow", False) or getattr(args, "shadow_profile", None) is not None \
            or getattr(args, "shadow_every", None) is not None \
            or getattr(args, "shadow_escalate", None) is not None \
            or getattr(args, "shadow_file_issues", None) is not None:
        from ..config import settings_form
        try:
            shadow = settings_form.shadow_block(
                loop.get("shadow"), "off" if args.no_shadow else args.shadow_profile,
                args.shadow_every,
                "--shadow-profile/--shadow-every/--shadow-escalate/--shadow-file-issues",
                escalate=getattr(args, "shadow_escalate", None),   # "on" with --no-shadow: refused
                file_issues=getattr(args, "shadow_file_issues", None))
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            raise _Refused
        if shadow is not None and shadow != (loop.get("shadow") or {}):
            changes["shadow"] = shadow
    if getattr(args, "review_only", None) is not None or getattr(args, "no_review_only", False):
        try:
            names = lists.check_review_only([] if args.no_review_only else args.review_only,
                                             loop, "--review-only")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            raise _Refused
        if names != (loop.get("review_only") or []):
            changes["review_only"] = names
    if getattr(args, "operator_agent", None) is not None or getattr(args, "no_operator_agents", False):
        try:
            agents = lists.check_operator_agents(
                [] if args.no_operator_agents else args.operator_agent, loop, "--operator-agent")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            raise _Refused
        if agents != (loop.get("operator_agents") or []):
            changes["operator_agents"] = agents


def _limit_changes(args, loop: dict, changes: dict) -> None:
    """The caps and the check names; ``0`` or blank removes a cap."""
    if getattr(args, "ci_fix_cap", None) is not None:
        try:
            value = (None if str(args.ci_fix_cap).strip() in ("0", "")
                     else lists.check_ci_fix_cap(args.ci_fix_cap, "--ci-fix-cap"))
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            raise _Refused
        if value != loop.get("ci_fix_cap"):
            changes["ci_fix_cap"] = value
    for key in ("review_only_cap", "review_only_daily"):
        wanted = getattr(args, key, None)
        if wanted is None:
            continue
        try:
            value = None if str(wanted).strip() in ("0", "") else lists._check_review_only_limit(
                wanted, key, f"--{key.replace('_', '-')}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            raise _Refused
        if value != loop.get(key):
            changes[key] = value
    # '' is a real value here: it clears the check.
    if getattr(args, "fixer_check", None) is not None:
        try:
            check = lists.check_fixer_check(args.fixer_check, "--fixer-check")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            raise _Refused
        if check != (loop.get("fixer_check") or ""):
            changes["fixer_check"] = check

    if getattr(args, "merge_check", None) and args.merge_check != (loop.get("merge_check") or "auto"):
        changes["merge_check"] = args.merge_check
    if getattr(args, "ingress", None) and args.ingress != (loop.get("ingress") or "repo-hooks"):
        changes["ingress"] = args.ingress
    if getattr(args, "ingress_app", None) is not None and args.ingress_app.strip() != (loop.get("ingress_app") or ""):
        changes["ingress_app"] = args.ingress_app.strip()


def _credentials(args, loop: dict) -> tuple:
    """The reader's and adjudicator's logins and the token/App maps ``set`` leaves.

    Returns (tokens, apps, adj_before, adj_after, read_before, read_after, adj_changed,
    read_changed)."""
    # The adjudicator's optional comment identity and the reader. `set` maps only *their*
    # credentials: the seats' token files move through the settings form and `apply`, which owns
    # the in-flight rules.
    tokens = dict(loop.get("tokens") or {})
    apps = {k: dict(v) for k, v in (loop.get("apps") or {}).items()}
    adj_before = config.adjudicator_login(loop)
    adj_wanted = getattr(args, "adjudicator_login", None)
    adj_after = adj_before if adj_wanted is None else adj_wanted.strip()
    read_before = str(loop.get("read_token") or "")
    read_wanted = getattr(args, "read_token", None)
    read_after = read_before if read_wanted is None else read_wanted.strip()
    if read_wanted is not None and not read_after:
        print("refused: --read-token needs a login — the gates cannot read GitHub as nobody")
        raise _Refused
    for pair in getattr(args, "token", None) or []:
        split = cli_common._split_token_pair(pair)
        if split is None:
            raise _Refused
        login, path = split
        owner = next((name for name in (adj_after, read_after if read_wanted is not None else "")
                      if name and login.lower() == name.lower()), "")
        if not owner and triage_command._is_seat_or_reader_login(loop, login):
            print(f"refused: `set --token` maps the token file of the login named by --read-token "
                  f"or --adjudicator-login, or of an extra login such as a hook admin "
                  f"(--admin-token); {login!r} is a seat or the current reader — seat token files "
                  "move through the plugin settings and `apply`, and a reader moves with "
                  "--read-token")
            raise _Refused
        try:
            credentials.check_token_file(path, f"--token {login}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            raise _Refused
        for key in [k for k in tokens if str(k).lower() == login.lower()]:
            del tokens[key]
        for key in [k for k in apps if str(k).lower() == login.lower()]:
            del apps[key]               # one identity, one credential: a PAT replaces an App
        tokens[owner or login] = str(pathlib.Path(path.strip()).expanduser())
    # --app (#1163): the same owners as --token, a GitHub App identity instead of a PAT.
    for pair in getattr(args, "app", None) or []:
        split = cli_common._split_app_pair(pair)
        if split is None:
            raise _Refused
        login, spec = split
        owner = next((name for name in (adj_after, read_after if read_wanted is not None else "")
                      if name and login.lower() == name.lower()), "")
        if not owner and triage_command._is_seat_or_reader_login(loop, login):
            print(f"refused: `set --app` maps the App of the login named by --read-token or "
                  f"--adjudicator-login; {login!r} is a seat or the current reader — a seat's "
                  "credential moves through the plugin settings and `apply` (or `init --app`), "
                  "and a reader moves with --read-token")
            raise _Refused
        for key in [k for k in apps if str(k).lower() == login.lower()]:
            del apps[key]
        for key in [k for k in tokens if str(k).lower() == login.lower()]:
            del tokens[key]             # an App replaces a PAT
        apps[owner or login] = spec
    # Preserve the mapping's spelling for consistent display; login lookup is case-insensitive.
    read_after = next((str(k) for k in (*tokens, *apps) if str(k).lower() == read_after.lower()),
                      read_after)
    tokens_changed = (tokens != (loop.get("tokens") or {})
                      or apps != (loop.get("apps") or {}))
    adj_changed = adj_after != adj_before or tokens_changed
    read_changed = (read_after != read_before
                    or cli_common._credential_label({"tokens": tokens, "apps": apps}, read_after)
                    != cli_common._credential_label(loop, read_after))
    return (tokens, apps, adj_before, adj_after, read_before, read_after, adj_changed,
            read_changed)


def _seat_capacity(args, loop: dict, seats: dict, seat_changes: dict, adj_before: str,
                   adj_after: str) -> None:
    """The adjudicator seat's login and each seat's own concurrency."""
    if adj_after != adj_before:
        adj_seat = dict(seats.get("adjudicator") or {})
        if adj_after:
            if not (loop.get("adjudicator") or {}).get("route"):
                print("refused: this loop has no adjudicator route, so nothing rules — an "
                      "adjudicator login would never post; turn adjudication on first with "
                      f"`hermes dk set --loop {loop['id']} --adjudicator-profile PROFILE`")
                raise _Refused
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
    # The shadow and triage seats' own capacity: no loop default reaches them (seat_concurrency),
    # so a value is always the seat's own. normalize refuses anything below 1, as the loader does.
    for seat in ("shadow", "triage"):
        value = getattr(args, f"{seat}_concurrency", None)
        if value is not None and value != config.seat_concurrency(loop, seat):
            seats.setdefault(seat, {})["concurrency"] = value
            seat_changes[seat] = value


def _seat_budgets(args, loop: dict, seats: dict, changes: dict) -> dict:
    """Each seat's turn budget, daily caps and steps, as ``{label: (was, now)}``."""
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
    return budget_changes


def _observer_block(args, loop: dict) -> dict:
    """The observer block the flags leave."""
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
                raise _Refused
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
            raise _Refused
    if args.observer_mute:
        observer_cfg["mute"] = True
    if args.observer_unmute:
        observer_cfg["mute"] = False
    return observer_cfg


def _validated(loop: dict, changes: dict, seats: dict, observer_cfg: dict, tokens: dict,
               apps: dict, read_after: str, read_changed: bool, adj_changed: bool) -> tuple:
    """The normalized loop, its credentials checked; returns (updated, app_route)."""
    try:
        updated = config.normalize({**loop, **changes, "seats": seats, "observer": observer_cfg,
                                    "tokens": tokens, "apps": apps, "read_token": read_after})
        if read_changed:
            # The same rules init applies to the reader: mapped, its file present and private,
            # and its own account (the four-identity rule). Its file is a PAT or an App key.
            if not cli_common._credential_file(updated, read_after):
                raise config.ConfigError(
                    f"no token file mapped for the reader {read_after!r} — add "
                    f"--token {read_after}=/path/to/pat, or --app {read_after}=APP_ID:/path/to/key.pem "
                    "for a GitHub App reader")
            credentials.check_token_file(cli_common._credential_file(updated, read_after),
                                    f"credential file for the reader {read_after!r}")
            credentials.verify_credentials(updated, {"read"})
        if adj_changed and config.adjudicator_login(updated):
            # The same file-level rules init applies, plus: the file must exist and be private.
            credentials.verify_adjudicator_token(updated)
            credentials.verify_credentials(updated)
        cli_common._observer_check(updated)
        # #1239: a loop switched onto an App's webhook gets that App's route now, not at the
        # next init; checked first, like every route this command writes.
        app_route = config.app_route(updated)
        if app_route and app_route != config.app_route(loop):
            config.webhook_host(updated.get("host"), required=True)
            cli_common._verify_routes(updated, {"app"})
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        raise _Refused
    return updated, app_route


def _observer_move(loop: dict, updated: dict) -> tuple:
    """Whether the observer's destinations move, refused while notices are unsettled; marks a
    disabled feed's tombstone. Returns (before, after, destination_changed, urgent_changed)."""
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
            raise _Refused
        if outstanding:
            print(f"refused: {outstanding} observer notice(s) are unsettled; retry/resolve "
                  "them before changing the route, profile, delivery or host")
            raise _Refused
    if after:
        updated.pop("observer_disabled", None)
    elif before:
        updated["observer_disabled"] = old_target
    return before, after, destination_changed, urgent_changed


def _observer_route(loop: dict, updated: dict, before: dict, after: dict,
                    destination_changed: bool) -> bool:
    """Write the observer's route and its intent when its destination moves; False when a
    shim could not be written."""
    shims_ok = True
    if destination_changed and after:
        name = after["route"]
        reserved = {cfg.get("route") for cfg in loop["seats"].values()}
        reserved.add((loop.get("adjudicator") or {}).get("route"))
        if name in reserved:
            print("refused: observer route must not replace a seat or adjudicator route")
            raise _Refused
        existing = routes.route(name)
        if existing and name != before.get("route"):
            print(f"refused: route {name!r} already exists and is not this observer's route")
            raise _Refused
        try:
            host = config.webhook_host(updated.get("host"), required=True)
            written = routes.new_route(name, profile=config.seat_profile(updated, "observer"),
                                       prompt=prompts.OBSERVER,
                                       events=["pull_request"], script="observe.py",
                                       deliver=after["deliver"], deliver_only=True, host=host,
                                       description=f"{updated['repo']} — read-only observer feed")
        except (OSError, ValueError, config.ConfigError) as exc:
            print(f"observer route could not be reconciled; loop config unchanged: {exc}")
            raise _Refused
        try:
            route_intent.record(updated, {name: written})
        except (OSError, ValueError) as exc:
            # Without the record, self-heal would put the *old* route back: undo the route too.
            try:
                routes.restore_entries({name: existing})
            except (OSError, ValueError) as rollback_exc:
                print(f"ROLLBACK FAILED: {rollback_exc} — inspect route {name!r} manually")
            print(f"observer route intent could not be recorded; loop config unchanged: {exc}")
            raise _Refused
        shims_ok = cli_common._install_shims(updated)
    return shims_ok


def _urgent_route(loop: dict, updated: dict, before: dict, after: dict, urgent_changed: bool,
                  shims_ok: bool) -> bool:
    """Write the observer's urgent route when it changes."""
    if urgent_changed and after.get("urgent_route"):
        urgent = after["urgent_route"]
        reserved = {cfg.get("route") for cfg in loop["seats"].values()}
        reserved |= {(loop.get("adjudicator") or {}).get("route"), after.get("route")}
        existing = routes.route(urgent)
        if urgent in reserved or (existing and urgent != before.get("urgent_route")):
            print(f"refused: route {urgent!r} is already in use and is not this observer's "
                  "urgent route")
            raise _Refused
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
            raise _Refused
        shims_ok = cli_common._install_shims(updated) and shims_ok
    return shims_ok


def _app_route(loop: dict, updated: dict, app_route: str) -> list:
    """Install the App's route with the config (#1239), both restored on failure; returns the
    shim lines to print."""
    shim_lines = []
    if app_route and app_route != config.app_route(loop):
        loop_file = config.config_dir() / f"{updated['id']}.json"
        previous_config = loop_file.read_bytes()
        previous_route = routes.route(app_route)
        try:
            cli_common._write_config(updated)
            cli_common._install_routes(updated, roles=("app",))
            # The route runs gate_app.py from the default profile's scripts: without its shim
            # the gateway resolves no script and drops every delivery.
            shim_lines = gate_shims.install(updated, report=True)
            route_intent.record_live(updated, [app_route])
        except Exception as exc:
            try:
                routes.restore_entries({app_route: previous_route})
                cli_common._restore_config(loop_file, previous_config)
            except Exception as rollback_exc:
                print(f"ROLLBACK FAILED: {rollback_exc} — inspect {loop_file} and route {app_route}")
                raise _Refused
            print(f"App route install FAILED: {exc}; config and route restored")
            raise _Refused
    return shim_lines


def _retire_routes(updated: dict, before: dict, after: dict, destination_changed: bool) -> None:
    """Remove the observer routes this change replaced; a failure only warns."""
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


def _print_summary(loop: dict, updated: dict, path, changes: dict, seat_changes: dict,
                   budget_changes: dict, read_before: str, read_after: str, adj_before: str,
                   adj_after: str, app_route: str, shim_lines: list) -> None:
    """What changed, then what to run next."""
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
    if read_after and cli_common._credential_label(loop, read_after) != cli_common._credential_label(updated, read_after):
        # A PAT path, or a GitHub App's id and key path (#1163) — never a value.
        print(f"  reader credential ({read_after}): {cli_common._credential_label(loop, read_after) or '(none)'}"
              f" → {cli_common._credential_label(updated, read_after)}")
    if adj_after != adj_before:
        shown = adj_after or "(none — rulings go to the operator only)"
        print(f"  adjudicator login: {adj_before or '(none)'} → {shown}")
    if adj_after and cli_common._credential_label(loop, adj_after) != cli_common._credential_label(updated, adj_after):
        print(f"  adjudicator credential ({adj_after}): {cli_common._credential_label(loop, adj_after) or '(none)'}"
              f" → {cli_common._credential_label(updated, adj_after)}")
    if updated.get("observer") != (loop.get("observer") or {}):
        print(f"  observer: {observer.describe(loop.get('observer') or {})} → "
              f"{observer.describe(updated.get('observer') or {})}")
    print(f"loop config updated: {path}")
    if app_route and app_route != config.app_route(loop):
        print(f"  route written: {app_route} ({updated.get('ingress_app')}'s events)")
        for line in shim_lines:
            print(f"  {line}")
        print(f"  next: `hermes dk app-webhook --loop {updated['id']} --set` points the App's "
              "webhook at it")
        if not config.app_ingress(loop):
            # Its repo hooks are not deleted here; the gates drop what they still deliver
            # (app_ingress.woken_by_repo_hook), and doctor names them until they are gone.
            print(f"  note: the loop's repo hooks stay until you delete them; the gates now drop "
                  f"what they deliver, and `hermes dk doctor --loop {updated['id']}` lists them")
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


def cmd_set(args) -> int:
    """Change a loop's settings in place, through the same validation ``init`` uses.

    Seat prompts are rendered from the payload at fire time; observer destination changes also
    reconcile the delivery-only route before updating config. The rails still apply — ``concurrency``
    above 1 without a ``clone`` (and without the isolated worker's runtime file) is refused here
    exactly as it is at init, because a parallel run that cannot be isolated would share a checkout.
    """
    try:
        return _set(args)
    except _Refused:
        return 2


def _set(args) -> int:
    """``cmd_set``'s steps, in order; a step that refuses raises ``_Refused``."""
    loop = _load_loop(args)
    adj_done = False
    if (getattr(args, "adjudicator_profile", None) or getattr(args, "adjudicator_route", None)
            or getattr(args, "adjudicator", None)):
        rc = set_adjudication._set_adjudication(args, loop)
        if rc:
            return rc
        adj_done = True
        loop = config.load_id(args.loop)

    host = _host_arg(args)
    changes = _flag_changes(args, loop, host)
    _list_changes(args, loop, changes)
    _limit_changes(args, loop, changes)
    seats = {seat: dict(cfg) for seat, cfg in loop["seats"].items()}
    seat_changes = {}

    (tokens, apps, adj_before, adj_after, read_before, read_after, adj_changed,
     read_changed) = _credentials(args, loop)
    _seat_capacity(args, loop, seats, seat_changes, adj_before, adj_after)
    budget_changes = _seat_budgets(args, loop, seats, changes)
    observer_cfg = _observer_block(args, loop)

    if (not changes and not seat_changes and not budget_changes and not adj_changed
            and not read_changed
            and observer_cfg == (loop.get("observer") or {})):
        if adj_done:
            return 0
        print("nothing to change — pass at least one setting "
              "(--concurrency, --reviewer-concurrency, --fixer-concurrency, "
              "--shadow-concurrency, --triage-concurrency, --cap, --clone, "
              "--turn-budget, "
              "--observer-profile, ...)")
        return 0

    updated, app_route = _validated(loop, changes, seats, observer_cfg, tokens, apps, read_after,
                                    read_changed, adj_changed)
    before, after, destination_changed, urgent_changed = _observer_move(loop, updated)
    shims_ok = _observer_route(loop, updated, before, after, destination_changed)
    shims_ok = _urgent_route(loop, updated, before, after, urgent_changed, shims_ok)
    shim_lines = _app_route(loop, updated, app_route)
    path = cli_common._write_config(updated)
    _retire_routes(updated, before, after, destination_changed)
    _print_summary(loop, updated, path, changes, seat_changes, budget_changes, read_before,
                   read_after, adj_before, adj_after, app_route, shim_lines)
    for line in cli_common._parallel_lines(updated):
        # The seat notes only when the loop default moved: that is when they explain something.
        if "concurrency" in changes or not line.startswith("note:"):
            print(f"  {line}")
    return 0 if shims_ok else 1
