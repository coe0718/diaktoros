"""``hermes dk set``'s adjudication change: ``--adjudicator-profile``/``--adjudicator-route``
turn ruling on, ``--adjudicator off`` turns it off (#1349, split from ``set_command``).
"""
from __future__ import annotations

from .. import (
    config,
    gate_shims,
    route_intent,
    routes,
)
from . import common as cli_common
from ..config import credentials
from ..config import settings_form


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
