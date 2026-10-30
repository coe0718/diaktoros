"""The route checks (#1349, split from the doctor core): each seat's route in the gateway
registry exists and wakes the profile, prompt and secret this loop needs (``check_routes``,
``check_route``), the adjudicator's and the observer's routes likewise, and the exact repair
command for each mismatch (``_apply_fix``, ``_prompt_fix``, …). Read-only, like every doctor
check. Each reaches the core through ``doctor`` (``from .. import doctor``), so a patch on a
core name reaches it too; callers use ``route_checks.<name>``.
"""
from __future__ import annotations

import json

from .. import config, doctor, observer, route_intent, routes


def _route_entry(data: dict, name: str) -> dict | None:
    entry = data.get(name)
    return entry if isinstance(entry, dict) else None


def check_routes(loop: dict) -> list[doctor.Check]:
    """Route/profile/secret correspondence, read from the gateway's own subscription file."""
    path = routes.subs_path()
    if not path.exists():
        return [doctor.Check("routes", doctor.ABSENT, f"no route registry at {path}", _missing_route_fix(loop))]
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return [doctor.Check("routes", doctor.MISMATCH, f"{path} is not readable JSON ({exc})",
                             "repair the subscription file: the gateway can route nothing out of it")]
    if not isinstance(data, dict):
        return [doctor.Check("routes", doctor.MISMATCH, f"{path} must hold a JSON object of routes",
                             "repair the subscription file: the gateway can route nothing out of it")]
    checks = [check_route(loop, data, seat) for seat in config.hook_roles(loop)]
    adjudicator = check_adjudicator_route(loop, data)
    if adjudicator:
        checks.append(adjudicator)
    feed = check_observer_route(loop, data)
    if feed:
        checks.append(feed)
    return _intent_overlay(loop, data, checks)


REPAIR_FIX = ("the next armed watchdog sweep restores it from the plugin's intent record with the "
              "same secret — or run `hermes dk doctor --repair` now (that one command "
              "repairs it); if the change was intended, make it with the plugin's own commands "
              "(`set`, `apply`, `uninstall`), not by hand")


def _intent_overlay(loop: dict, data: dict, checks: list[doctor.Check]) -> list[doctor.Check]:
    """Compare the live registry with the plugin's own record of its routes (issue #1).

    Read-only, like every check here. A route another registry writer changed can still look
    well-formed (a fresh secret, say) — only the record knows it no longer matches GitHub's hook.
    With no record yet (an install that predates it) the route checks stand as they are.
    """
    try:
        intent = route_intent.load(loop)
    except route_intent.IntentError as exc:
        return checks + [doctor.Check("route-intent", doctor.MISMATCH, str(exc),
                                      "restore the file, or re-run `hermes dk apply` so the "
                                      "plugin records its routes again")]
    if intent is None:
        return checks
    drifted = route_intent.drift(loop, data, intent)
    by_name = {check.name: check for check in checks}
    for name in sorted(set(intent) & set(route_intent.routes_of(loop).values())):
        check = by_name.get(f"route:{name}")
        fields = drifted.get(name)
        if check is None:
            if fields:
                checks.append(doctor.Check(f"route:{name}", doctor.MISMATCH,
                                           "differs from the plugin's intent record: " + ", ".join(fields),
                                           REPAIR_FIX))
            continue
        if not fields:
            if check.status == doctor.VERIFIED:
                check.detail += " · matches intent record"
            continue
        what = ("erased by another registry writer" if fields == ["missing"]
                else "differs from the plugin's intent record: " + ", ".join(fields))
        if check.status == doctor.VERIFIED:
            check.status, check.detail = doctor.MISMATCH, what
        else:
            check.detail += f" ({what})"
        live = data.get(name)
        if isinstance(live, dict) and not route_intent.owned(live):
            # Repair reports a route something else now runs as a conflict and never overwrites
            # it, so it can only be the second step.
            check.fix = (f"remove or rename that entry (it now runs {live.get('script')!r}, not a "
                         "Diaktoros gate, so repair and apply leave it alone), then "
                         f"`hermes dk doctor --loop {loop['id']} --repair`")
        # The observer check already chose between repair and a rebuild: repair only restores
        # the record, which is no fix when the record itself breaks the feed's contract.
        elif not (check.fix and name == (loop.get("observer") or {}).get("route")):
            check.fix = REPAIR_FIX
    return checks


def _missing_route_fix(loop: dict) -> str:
    """``init`` refuses an existing loop, so a lost route is written back by ``apply``. (With an
    intent record, ``_intent_overlay`` replaces this with ``doctor --repair``, same secret.)"""
    from .. import gate_shims
    return gate_shims.recreate_fix(loop)


def _apply_fix(loop: dict, what: str) -> str:
    """``apply`` rewrites this loop's own routes from its config (secret kept): the remedy for
    every route field it reconciles. Never ``init``, which refuses an existing loop."""
    return f"run `hermes dk apply --loop {loop['id']}` to {what} (its secret is kept)"


def _secret_fix(loop: dict) -> str:
    """A route with no secret cannot be rewritten in place: apply keeps the secret it finds, and
    a new one would not match the repo hook. Recreating it mints one and re-keys the hook."""
    return (f"remove that entry (a route without a secret can never verify a signature), then "
            f"{_missing_route_fix(loop)}")


def _host_fixes(loop: dict) -> tuple[str, str]:
    """(invalid origin, no origin): both name the loop's origin with ``set``, then ``apply``."""
    lid = loop["id"]
    both = (f"`hermes dk set --loop {lid} --host https://your-gateway.example`, then "
            f"`hermes dk apply --loop {lid}` so the routes carry it")
    return both, both


def _prompt_fix(loop: dict, name: str) -> str:
    """The remedy for a route whose prompt no longer proves it is ours: the same one apply prints
    (``gate_shims.divergence``) — repair from the record when it holds the real prompt, else
    move the entry aside and recreate it."""
    from .. import gate_shims
    found = gate_shims.divergence(loop, contract=True).get(name)
    return found[1] if found else REPAIR_FIX


def check_route(loop: dict, data: dict, seat: str) -> doctor.Check:
    name = str(route_intent.routes_of(loop).get(seat) or "")
    profile = config.seat_profile(loop, seat)
    if not name:
        return doctor.Check(f"route:{seat}", doctor.ABSENT, "no route named for this seat",
                            f"name the route as seats.{seat}.route in the loop config, then "
                            f"`hermes dk apply --loop {loop['id']} --recreate-routes`")
    entry = _route_entry(data, name)
    if entry is None:
        return doctor.Check(f"route:{name}", doctor.ABSENT, f"not in {routes.subs_path().name}",
                            _missing_route_fix(loop))
    if routes.route_profile(entry) != profile:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            f"wakes profile {entry.get('profile')!r}, but seats.{seat}.profile is "
                            f"{profile!r} — the wake would run the wrong agent",
                            _apply_fix(loop, f"rebind it to {profile or 'the configured profile'}"))
    if entry.get("deliver_only"):
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            "deliver_only is set — the gateway delivers the rendered prompt and runs no "
                            "agent, so this seat is never woken",
                            f"run `hermes dk apply --loop {loop['id']}` to rewrite it from the "
                            "loop config (its secret is kept)")
    if entry.get("enabled", True) is False:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            "disabled in the registry (enabled: false) — the gateway answers 403 to "
                            "every event, so this seat is never woken",
                            f"run `hermes dk apply --loop {loop['id']}` to re-enable it (its "
                            "secret is kept)")
    if not str(entry.get("secret") or ""):
        return doctor.Check(f"route:{name}", doctor.ABSENT, "registered without a secret",
                            _secret_fix(loop))
    if not str(entry.get("prompt") or ""):
        return doctor.Check(f"route:{name}", doctor.ABSENT, "registered without a prompt",
                            _prompt_fix(loop, name))
    if entry.get("prompt") != route_intent.ROUTE_PROMPT[seat]:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            f"does not carry the {seat} gate's prompt — the wake would run an agent on "
                            "another protocol, and nothing proves the route is this plugin's",
                            _prompt_fix(loop, name))
    script = str(entry.get("script") or "")
    if script != doctor.GATE_SCRIPT[seat]:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            f"runs gate script {script or '(none)'!r}, expected "
                            f"{doctor.GATE_SCRIPT[seat]!r}",
                            _prompt_fix(loop, name))
    events = entry.get("events")
    if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
        return doctor.Check(f"route:{name}", doctor.MISMATCH, "events must be a list of event names",
                            _apply_fix(loop, f"rewrite it with the {seat} gate's event"))
    if doctor.GATE_EVENT[seat] not in events:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            f"events {events or '(none)'} do not include {doctor.GATE_EVENT[seat]!r}",
                            _apply_fix(loop, f"rewrite it with {doctor.GATE_EVENT[seat]!r}: the gateway only "
                                       "routes the events a route subscribes to"))
    host = str(loop.get("host") or "")
    try:
        url = routes.url_for(name, host or None)
    except config.ConfigError:
        return doctor.Check(f"route:{name}", doctor.MISMATCH, "invalid webhook host (URL withheld)",
                            _host_fixes(loop)[0])
    if not url:
        return doctor.Check(f"route:{name}", doctor.ABSENT, "no webhook URL (neither the loop nor the route "
                            "names a gateway origin)",
                            _host_fixes(loop)[1])
    stored = str(entry.get("host") or "").removesuffix("/")
    if host and stored and stored != host:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            "registered gateway origin differs from the loop's configured origin (URLs withheld)",
                            _apply_fix(loop, "rewrite it at the loop's origin"))
    return doctor.Check(f"route:{name}", doctor.VERIFIED,
                        f"{profile} · {doctor.GATE_EVENT[seat]} · [webhook URL redacted]")


def check_adjudicator_route(loop: dict, data: dict) -> doctor.Check | None:
    """The escalation route must use the post-#21 adjudicator gate."""
    adjudicator = loop.get("adjudicator") or {}
    name = str(adjudicator.get("route") or "")
    if not name:
        return None
    profile = str(adjudicator.get("profile") or "default")
    entry = _route_entry(data, name)
    if entry is None:
        return doctor.Check(f"route:{name}", doctor.ABSENT, f"not in {routes.subs_path().name}",
                            _missing_route_fix(loop) + " — the breach marker is the only record of an "
                            "escalation nobody is woken for")
    if routes.route_profile(entry) != profile:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            f"wakes profile {entry.get('profile')!r}, but adjudicator.profile is "
                            f"{profile!r}",
                            _apply_fix(loop, f"rebind it to {profile}: the ruling must not happen as one "
                                       "of the two seats that just stalled"))
    if entry.get("deliver_only"):
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            "deliver_only is set — the gateway delivers the rendered prompt and runs no "
                            "agent, so no adjudicator is woken",
                            f"run `hermes dk apply --loop {loop['id']}` to rewrite it from the "
                            "loop config (its secret is kept)")
    if entry.get("enabled", True) is False:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            "disabled in the registry (enabled: false) — the gateway answers 403 to "
                            "every event, so no adjudicator is woken",
                            f"run `hermes dk apply --loop {loop['id']}` to re-enable it (its "
                            "secret is kept)")
    if not str(entry.get("secret") or ""):
        return doctor.Check(f"route:{name}", doctor.ABSENT, "registered without a secret",
                            _secret_fix(loop))
    if not str(entry.get("prompt") or ""):
        return doctor.Check(f"route:{name}", doctor.ABSENT, "registered without a prompt",
                            _prompt_fix(loop, name))
    if entry.get("prompt") != route_intent.ROUTE_PROMPT["adjudicator"]:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            "does not carry the adjudicator gate's prompt — the ruling would run on "
                            "another protocol, and nothing proves the route is this plugin's",
                            _prompt_fix(loop, name))
    events = entry.get("events")
    if not isinstance(events, list) or any(not isinstance(event, str) for event in events):
        return doctor.Check(f"route:{name}", doctor.MISMATCH, "events must be a list of event names",
                            _apply_fix(loop, "rewrite it with the adjudicator gate's event"))
    if "pull_request" not in events:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            f"events {events or '(none)'} do not include 'pull_request'",
                            _apply_fix(loop, "rewrite it with 'pull_request': breach wakes use it"))
    script = str(entry.get("script") or "")
    if script in config.LEGACY_GATE_SCRIPTS["adjudicator"]:
        # Installed before the dedicated adjudicator gate. `init` refuses an existing loop, so
        # the hint names the command that rewrites this route in place, secret kept.
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            f"runs {script!r} (installed by an older release), expected "
                            "'gate_adjudicator.py'",
                            f"run `hermes dk apply --loop {loop['id']}` to rebind it to "
                            "gate_adjudicator.py (its secret is kept)")
    if script != "gate_adjudicator.py":
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            f"runs {script or '(none)'!r}, expected 'gate_adjudicator.py'",
                            _prompt_fix(loop, name))
    if not (doctor.scripts_dir() / script).is_file():
        return doctor.Check(f"route:{name}", doctor.ABSENT, f"gate_adjudicator.py missing from {doctor.scripts_dir()}",
                            "reinstall the plugin")
    host = str(loop.get("host") or "").removesuffix("/")
    stored = str(entry.get("host") or "").removesuffix("/")
    if host and stored and stored != host:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            "registered gateway origin differs from the loop's configured origin (URLs "
                            "withheld)", _apply_fix(loop, "rewrite it at the loop's origin"))
    return doctor.Check(f"route:{name}", doctor.VERIFIED, f"{profile} · adjudication wake")


def check_observer_route(loop: dict, data: dict) -> doctor.Check | None:
    """The observer feed's route: present, serving the profile the loop names, and exactly the
    delivery-only contract the feed checks before every notice (``observer.route_contract``).

    Nothing to check when the loop has no feed. A misconfigured feed has no route to check, but
    it is still a feed that delivers nothing, so it is reported rather than skipped.
    """
    cfg = loop.get("observer") or {}
    if not cfg:
        return None
    if cfg.get("misconfigured"):
        return doctor.Check("route:observer", doctor.MISMATCH, str(cfg["misconfigured"]),
                            f"`hermes dk set --loop {loop['id']} --observer-profile <name>` "
                            "(or --observer-disable): the feed delivers nothing as configured")
    name = str(cfg.get("route") or "")
    profile = config.seat_profile(loop, "observer")
    remedy = observer.route_remedy(loop)
    entry = _route_entry(data, name)
    if entry is None:
        return doctor.Check(f"route:{name}", doctor.ABSENT, f"not in {routes.subs_path().name}",
                            f"{remedy} writes it again")
    served = routes.route_profile(entry)
    if served is None:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            f"profile {entry.get('profile')!r} is blank or not a name — the gateway "
                            f"refuses every request for it, and the feed refuses to deliver through it",
                            f"run `hermes dk apply --loop {loop['id']}` to rebind it to "
                            f"{profile} (its secret is kept)")
    if served != profile:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            f"wakes profile {served!r}, but observer.profile is {profile!r} — the "
                            "feed refuses to deliver through it",
                            f"run `hermes dk apply --loop {loop['id']}` to rebind it to "
                            f"{profile} (its secret is kept)")
    # The very comparison the feed makes before every notice (routes.target) — with the checks
    # above and below, this is exactly observer._target(): doctor verifies the route if and only
    # if the feed would deliver through it (tests sweep every registry field for that). Drift
    # from the plugin's intent record is the one stricter case, reported by _intent_overlay.
    contract = observer.route_contract(loop)
    wrong = [key for key in routes.contract_mismatch(entry, contract) if key != "profile"]
    if wrong:
        return doctor.Check(f"route:{name}", doctor.MISMATCH,
                            "does not match the observer delivery-only contract: " + ", ".join(wrong)
                            + " — the feed refuses to deliver through it", f"{remedy} restores it")
    if not str(entry.get("secret") or ""):
        return doctor.Check(f"route:{name}", doctor.ABSENT, "registered without a secret",
                            f"{remedy}: a notice without a secret cannot be signed")
    # The registry's own `host` is not checked: the feed always posts to the loop's origin and
    # never reads the registry's (a private PR link must not follow a registry edit), so a stale
    # one changes nothing about delivery.
    if not str(loop.get("host") or ""):
        return doctor.Check(f"route:{name}", doctor.ABSENT, "the loop names no gateway origin",
                            f"`hermes dk set --loop {loop['id']} --host "
                            "https://your-gateway.example`: the feed never borrows the registry's host")
    muted = " · muted" if cfg.get("mute") else ""
    return doctor.Check(f"route:{name}", doctor.VERIFIED,
                        f"{profile} · observer feed → {contract['deliver']} · deliver-only{muted}")
