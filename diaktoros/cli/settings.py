"""Changing a loop (#676): ``show`` and ``settings``. ``set``, ``apply``,
``triage``, ``identity`` and ``fixer-push`` are their own modules (#1349), and the route and
hook reconciliation they share is ``route_sync``.

``cli`` re-exports every command here. The install plumbing these share with ``init`` and
``uninstall``, the settings form's values and the branch-push refusal are ``cli_common``'s.
"""
from __future__ import annotations

import json

from .. import (
    config,
)
from . import common as cli_common
from . import route_sync
from ..config import settings_form


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
        if role in config.TOKEN_FILE_SETTINGS:
            print(f"  {'':<12} App id {entry.get('app_id') or '(blank)'}, "
                  f"key file {entry.get('app_key_file') or '(blank)'}")
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
                  + " · ".join(route_sync._role_summary(loop, role) for role in config.ROUTE_ROLES))
            refs = cli_common._credential_lines(loop)
            if refs:
                print(f"  {'':<18} token files: " + " · ".join(refs))
    if not cli_common._SETTINGS:
        print("\nnothing set — every value above is the schema default")
    print("\napply them to a loop with: hermes dk apply --loop <id>"
          "\n(settings are defaults, not a subscription: an existing loop keeps its own seats, "
          "profiles and numbers until you apply — and blank fields above never erase them)")
    return 0


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
