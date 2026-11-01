"""The gateway and GitHub-hook checks (#1349, split from the doctor core): is something
listening at the gateway (``check_gateway``, a TCP connect, never a POST), do the repo's hooks
(or the App's webhook) point at this loop's routes and wake the right seat (``check_hooks``,
``check_hook``, ``check_app_webhook``, ``check_leftover_hooks``), and what GitHub's last
deliveries say (``check_deliveries``). Read-only, like every doctor check. Each reaches the core
through ``doctor`` (``from .. import doctor``), so a patch on a core name reaches it too; callers
use ``hook_checks.<name>``.
"""
from __future__ import annotations

import re
import shlex
import socket
from urllib.parse import urlsplit

from .. import config, doctor, gh, route_intent, routes
from ..config import host_guards


def gateway_reachable(host: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Is anything listening at the gateway's origin? A TCP connect — never a webhook POST,
    because a POST at a seat's route starts a real agent run."""
    parsed = urlsplit(host)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    name = parsed.hostname or host
    # Under the test guard only a loopback gateway is probed; a real host raises first.
    host_guards.guard_network(f"tcp://{f'[{name}]' if ':' in name else name}:{port}")
    try:
        with socket.create_connection((name, port), timeout=timeout):
            return True, f"{name}:{port} accepts a connection"
    except Exception as exc:
        return False, f"{name}:{port} — {type(exc).__name__}: {exc}"


def check_gateway(loop: dict, offline: bool) -> doctor.Check:
    host = str(loop.get("host") or "")
    if not host:
        return doctor.Check("gateway", doctor.ABSENT, "no webhook host configured",
                            "pass --host https://your-gateway.example at init (or set the plugin's "
                            "webhook host): without one no route URL resolves and no hook can point at "
                            "this operator's gateway")
    if offline:
        return doctor.Check("gateway", doctor.UNKNOWN, "configured gateway not probed (--offline; URL withheld)")
    reachable, _ = gateway_reachable(host)
    if not reachable:
        return doctor.Check("gateway", doctor.ABSENT, "configured gateway unreachable (URL withheld)",
                            "start it (`hermes gateway status`, `hermes gateway start`), and check that "
                            "this origin is the one GitHub posts to; re-run with --offline to skip this "
                            "probe")
    return doctor.Check("gateway", doctor.VERIFIED, "configured gateway accepts a TCP connection (URL withheld)")


def _hook_listing(loop: dict) -> list | None:
    """The repo's complete hook listing, or None when it cannot all be read."""
    hooks = []
    for page in range(1, 1001):
        path = f"/repos/{loop['repo']}/hooks?per_page=100"
        batch = gh.api(loop, path if page == 1 else f"{path}&page={page}")
        if not isinstance(batch, list) or any(
            not isinstance(hook, dict) or not isinstance(hook.get("config"), dict)
            or not isinstance(hook["config"].get("url"), str)
            or not isinstance(hook.get("events"), list)
            or any(not isinstance(event, str) for event in hook["events"])
            or type(hook.get("active")) is not bool
            for hook in batch):
            return None
        hooks.extend(batch)
        if len(batch) < 100:
            return hooks
    return None


def check_app_webhook(loop: dict, offline: bool) -> doctor.Check:
    """#1239: the App's webhook, read with the App's JWT, must post to this loop's App route with
    every event it needs; until then the loop receives nothing, however healthy the rest reads."""
    from .. import app_ingress
    app = loop.get("ingress_app")
    fix = f"hermes dk app-webhook --loop {loop['id']} --set"
    if offline:
        return doctor.Check("app-webhook", doctor.UNKNOWN, f"not probed (--offline): {app}'s webhook was not read")
    state = app_ingress.webhook_state(loop)
    if state["error"]:
        return doctor.Check("app-webhook", doctor.UNKNOWN, state["error"], f"`{fix}` once the App's key reads")
    problem = app_ingress.webhook_problem(state)
    if problem:
        events = ("; subscribe to the missing events in the App's settings (Permissions & events)"
                  if state["missing_events"] else "")
        return doctor.Check("app-webhook", doctor.MISMATCH, f"{app}'s webhook delivers nothing here: {problem}",
                            f"`{fix}`{events}")
    return doctor.Check("app-webhook", doctor.VERIFIED, f"{app}'s webhook posts to {state['url']} "
                        f"({', '.join(state['events'])})")


def check_leftover_hooks(loop: dict, offline: bool) -> doctor.Check:
    """#1239: a loop on its App's webhook has no repo hooks on purpose. One left from before it
    switched still posts to the seat's route; the gate drops those deliveries
    (``app_ingress.woken_by_repo_hook``), but the hook should go."""
    source = (f"events arrive through {loop.get('ingress_app')}'s webhook (route "
              f"{config.app_route(loop)})")
    names = {str(name) for role, name in route_intent.routes_of(loop).items()
             if role in ("reviewer", "fixer", "triage") and name}
    if not routes.route(config.app_route(loop)):
        return doctor.Check("hooks", doctor.UNKNOWN, f"not checked: {source}, but that route is not installed "
                            "(see the route checks above)")
    if offline:
        return doctor.Check("hooks", doctor.UNKNOWN, f"not probed (--offline): {source}; leftover repo hooks "
                            "were not looked for")
    hooks = _hook_listing(loop)
    if hooks is None:
        return doctor.Check("hooks", doctor.UNKNOWN,
                            f"{source}; the repo's hooks could not all be read, so a leftover one to "
                            f"this loop's routes was not looked for",
                            f"{config.reader_access_fix(loop, 'hooks')} and re-run")
    left = sorted(int(hook.get("id") or 0) for hook in hooks
                  if hook_route_name(hook) in names)
    if left:
        ids = ", ".join(str(n) for n in left)
        return doctor.Check("hooks", doctor.MISMATCH,
                            f"{source}, but {len(left)} repo hook(s) still post to this loop's seat "
                            f"routes (id {ids}): the gates drop what they deliver",
                            f"delete them: `gh api -X DELETE repos/{loop['repo']}/hooks/<id>` for each "
                            f"id, or in repo settings → Webhooks")
    return doctor.Check("hooks", doctor.VERIFIED, f"none, on purpose: {source}")


def check_hooks(loop: dict, offline: bool) -> list[doctor.Check]:
    """The repo hooks GitHub posts to, one per role in ``config.hook_roles`` (reviewer, fixer, and
    triage when it is on), matched by the URL each route resolves to.

    A read the token is not allowed to make is ``unknown``: the repo may have all its hooks and
    none at all, and this call cannot tell those apart. Saying "absent" here would be the exact
    wrong-hook hunt the preflight exists to prevent.
    """
    if config.app_ingress(loop):
        return [check_leftover_hooks(loop, offline), check_app_webhook(loop, offline)]
    expected = []
    for seat in config.hook_roles(loop):
        name = str(route_intent.routes_of(loop).get(seat) or "")
        try:
            url = routes.url_for(name, str(loop.get("host") or "") or None) if name else None
        except config.ConfigError:
            url = None
        if url:
            expected.append((seat, name, url))
    if not expected:
        return [doctor.Check("hooks", doctor.UNKNOWN, "not checked — no route URL resolves yet "
                             "(see the route checks above)")]
    if offline:
        return [doctor.Check("hooks", doctor.UNKNOWN,
                             f"not probed (--offline) — {len(expected)} hook(s) were not read")]
    hooks = _hook_listing(loop)
    if hooks is None:
        return [doctor.Check("hooks", doctor.UNKNOWN,
                             f"could not read the complete /repos/{loop['repo']}/hooks listing — nothing was proved about "
                             f"{len(expected)} hook(s) (a reader without "
                             f"{config.reader_access_need(loop, 'hooks')} reads as denied)",
                             f"{config.reader_access_fix(loop, 'hooks')} and re-run; check by hand with "
                             f"`gh api repos/{loop['repo']}/hooks`")]
    checks = []
    for seat, name, url in expected:
        checks.append(check_hook(loop, hooks, seat, name, url))
    return checks


def hook_route_name(hook: dict) -> str:
    """The webhook route a hook posts to: the last ``/webhooks/<name>`` path segment, exactly.

    Never a substring test on the URL: route ``widgets`` must not claim a hook posting to
    ``widgets-fix``, nor a route name embedded anywhere else in a URL.
    """
    url = (hook.get("config") or {}).get("url") if isinstance(hook.get("config"), dict) else ""
    return routes.route_name_of(url)


def seat_route_target(loop: dict, name: str) -> tuple[str | None, str]:
    """``(url, "")`` for the one URL a hook must post to for route ``name`` to wake its seat, or
    ``(None, reason)`` when no hook can.

    The registry is the gateway's binding, so it is the only source: the gateway serves
    ``/webhooks/<route>`` (default profile) and ``/p/<profile>/webhooks/<route>`` for a route in
    its subscription file, and answers anything else 404 — another profile's URL included. A
    route missing from the registry has no binding at all; a route whose entry binds another
    profile than the seat's would run the wrong agent. Both are ``route_checks.check_route``'s findings,
    in its words, and neither has a URL a hook could be credited to.
    """
    role = next((role for role, route in route_intent.routes_of(loop).items() if route == name),
                "")
    entry = routes.route(name)
    if entry is None:
        return None, (f"route {name!r} is not in {routes.subs_path().name} — the gateway has no "
                      "binding for it, so no hook can wake this seat")
    want = config.seat_profile(loop, role) if role else ""
    if role in ("reviewer", "fixer") and str(entry.get("profile") or "") != want:
        return None, (f"route {name!r} wakes profile {entry.get('profile')!r}, but "
                      f"seats.{role}.profile is {want!r} — the wake would run the wrong agent")
    url = routes.url_for(name, str(loop.get("host") or "") or None)
    if not url:
        return None, f"route {name!r} has no gateway host to build its URL from"
    return url, ""


def seat_hook_url(loop: dict, name: str) -> str | None:
    """The registry URL a hook must post to for route ``name`` (see ``seat_route_target``)."""
    return seat_route_target(loop, name)[0]


def same_hook_url(posted: str, expected: str) -> bool:
    """Is this hook *for* that route URL? Scheme and host case-insensitive, a trailing slash
    ignored. For finding a hook (whose is it, which one to pause or delete) — never for deciding
    it is correct: see ``exact_hook_url``."""
    return routes.same_webhook_url(posted, expected)


def exact_hook_url(posted: str, expected: str) -> bool:
    """Does the gateway route this hook's URL to that route? Path byte-exact.

    The gateway registers ``/webhooks/{route_name}`` and ``/p/{profile}/webhooks/{route_name}``
    only (``{route_name}`` cannot hold a ``/``, and no path normalizing is installed), so
    ``…/webhooks/<route>/`` falls through to the ``/p/{profile}/{tail}`` catch-all and is
    answered 404. Scheme and host still compare case-insensitively (DNS and the Host header),
    and a query string is not part of the match: the router reads the path only, so
    ``…/webhooks/<route>?x=1`` is delivered like the bare URL.
    """
    return routes.serves_route_url(posted, expected)


SLASH_404 = ("posts to the route's URL with a trailing slash — the gateway does not route it "
             "(404), so this seat is never woken")


def _profile_in(path: str) -> str:
    """The profile a webhook URL path names (``default`` when it has no ``/p/<profile>``)."""
    head = path.rsplit("/webhooks/", 1)[0].strip("/")
    return head[2:] if head.startswith("p/") else ("default" if not head else "")


def hook_url_difference(posted: str, expected: str) -> str:
    """Why ``posted`` is not the route's own URL, in the operator's terms (no URL echoed)."""
    if not expected:
        return "a route with no registry binding (see the route's line)"
    got, want = urlsplit(posted.rstrip("/")), urlsplit(expected.rstrip("/"))
    if (got.scheme.lower(), got.netloc.lower()) != (want.scheme.lower(), want.netloc.lower()):
        return f"another origin ({got.scheme}://{got.netloc.lower()}, not this loop's gateway)"
    have, need = _profile_in(got.path), _profile_in(want.path)
    if have and need and have != need and got.path.endswith(want.path.rsplit("/webhooks/", 1)[-1]):
        return (f"another profile ({have!r}; the route is bound to {need!r}, and the gateway "
                "answers any other profile's URL 404)")
    return "another path on this gateway (not the route's URL)"


def install_hook_urls(loop: dict, name: str) -> list[str]:
    """Every URL a hook *this install* made (or is about to make) for route ``name`` posts to.

    An ownership question, not a delivery one: pausing (``arm --pause``), ``uninstall``
    deleting its hooks (``cli_common._classify_hooks``) and ``init``'s stale-hook guard (the same
    function) ask "is this hook one of ours?", and the answer includes the route's registry URL
    whatever profile it binds, and the URL the loop's config gives it — which is all there is
    once the registry entry is gone (a route removed before its hooks, or for ``init`` not
    written yet). Arming never uses this: whether a hook *wakes the
    seat* is ``seat_route_target``'s question, and only the registry binding answers it.
    """
    urls = []
    host = str(loop.get("host") or "") or None
    if routes.route(name) is not None:
        registered = routes.url_for(name, host)
        if registered:
            urls.append(registered)
    role = next((role for role, route in route_intent.routes_of(loop).items() if route == name),
                "")
    if role:
        planned = routes.url_for_profile(name, config.seat_profile(loop, role), host)
        if planned and not any(same_hook_url(planned, url) for url in urls):
            urls.append(planned)
    return urls


def hook_wake_problem(hook: dict, seat: str) -> str:
    """Why a hook at the seat's own URL still would not wake it, in ``check_hook``'s words."""
    event = doctor.GATE_EVENT.get(seat)
    events = hook.get("events")
    if not isinstance(events, list) or event not in [str(item) for item in events]:
        return (f"subscribes to {events if isinstance(events, list) and events else '(no events)'}"
                f", not {event!r}")
    content_type = (hook.get("config") or {}).get("content_type")
    if content_type != "json":
        return f"has content_type {content_type!r}, expected 'json'"
    return ""


def split_route_hooks(loop: dict, listing: list, names, *,
                      ownership: bool = False) -> tuple[list[dict], list[dict]]:
    """``(own, other)`` for the hooks posting to one of the route ``names``.

    The one matcher for hooks: ``arm`` (arming and pausing), ``uninstall`` and ``init``'s
    stale-hook guard (via ``cli_common._classify_hooks``) and ``selftest --ping``; ``doctor``'s helpers
    use the same URL rules. A hook is a
    seat's only when it posts to exactly that route's registry URL, and the registry entry binds
    the seat's profile (``seat_route_target``). Every other hook
    whose last ``/webhooks/<route>`` segment names one of the routes — another profile's URL, a
    retired gateway, another install — is *other*: reported, never flipped, deleted or pinged
    as this loop's. ``ownership=True`` (pausing, uninstall, init's stale-hook guard) counts the
    URLs ``install_hook_urls`` names instead. Raises ``ConfigError`` when the loop has no usable
    host.
    """
    config.webhook_host(loop.get("host"), required=True)
    if ownership:
        expected = {name: install_hook_urls(loop, name) for name in set(names)}
    else:
        expected = {name: [url] if (url := seat_hook_url(loop, name)) else []
                    for name in set(names)}
    own, other = [], []
    for hook in listing:
        if not isinstance(hook, dict):
            continue
        name = hook_route_name(hook)
        if name not in expected:
            continue
        posted = str((hook.get("config") or {}).get("url") or "")
        mine = any(same_hook_url(posted, want) for want in expected[name])
        (own if mine else other).append(hook)
    return own, other


def check_hook(loop: dict, hooks: list, seat: str, name: str, url: str) -> doctor.Check:
    event = doctor.GATE_EVENT[seat]
    def posted_url(hook: dict) -> str:
        cfg = hook.get("config")
        return str(cfg.get("url") or "") if isinstance(cfg, dict) else ""

    exact = [hook for hook in hooks if same_hook_url(posted_url(hook), url)]
    # A wrong origin/profile/path for the same webhook route is a mismatch (reported with its
    # cause), not an absent hook — "the same route" is the exact segment, never a substring.
    candidates = exact or [hook for hook in hooks if hook_route_name(hook) == name]
    # Every hook posting to this route name, on any origin. More than one is a previous install
    # left behind: GitHub never returns a secret, but a leftover signs with the secret the old
    # route held, so at most one of them can authenticate — and "hook N active" says nothing
    # about which one that is.
    # Only this gateway's hooks can be duplicates: the same route name on another origin is
    # another install (or an old gateway) and never receives this route's deliveries.
    # A duplicate is a second hook at the route's own URL; one at another profile, path or
    # origin never reaches this route at all (the gateway answers it 404, or it goes elsewhere).
    named = list(exact)
    if len(named) > 1:
        ids = sorted(hook.get("id") for hook in named if isinstance(hook.get("id"), int))
        active = sum(1 for hook in named if hook.get("active"))
        repo = loop["repo"]
        deletes = "; ".join(f"`gh api -X DELETE {shlex.quote(f'repos/{repo}/hooks/{i}')}`"
                            for i in ids[:-1])
        return doctor.Check(f"hook:{name}", doctor.MISMATCH,
                            f"{len(named)} repo hooks post to this route (ids "
                            f"{', '.join(str(i) for i in ids)}; {active} active) — duplicates from a "
                            "previous install sign with a secret this route no longer holds, so their "
                            "deliveries are refused",
                            f"`hermes dk uninstall --loop {shlex.quote(loop['id'])}` deletes "
                            "them all, then re-run init --hooks; or keep only the newest (GitHub ids "
                            f"only grow, so {ids[-1]} is the latest init's) and delete the rest: {deletes}")
    match = next((hook for hook in candidates if hook.get("active") and
                  event in (hook.get("events") or [])), None) or (candidates[0] if candidates else None)
    if match is None:
        return doctor.Check(f"hook:{name}", doctor.ABSENT, "no repo hook posts to [webhook URL redacted]",
                            doctor.hooks_fix(loop, "creates it, paused until `arm`") + ", or add the hook "
                            "by hand with that URL and the route's secret")
    hook_id = match.get("id")
    posted = posted_url(match)
    if not same_hook_url(posted, url):
        why = hook_url_difference(posted, url)
        return doctor.Check(f"hook:{name}", doctor.MISMATCH,
                            f"hook {hook_id} posts to {why}, not [webhook URL redacted]",
                            doctor.hooks_fix(loop, f"repoints hook {hook_id} at the route's URL (secret and TLS "
                                             "setting kept): the gateway delivers this route only at that "
                                             "URL, so the hook wakes nothing"))
    if not exact_hook_url(posted, url):
        return doctor.Check(f"hook:{name}", doctor.MISMATCH, f"hook {hook_id} {SLASH_404}",
                            f"`hermes dk apply --loop {loop['id']}` repoints hook {hook_id} at "
                            "the exact URL (or edit its URL on GitHub to drop the trailing slash)")
    events = [str(item) for item in (match.get("events") or [])]
    if event not in events:
        return doctor.Check(f"hook:{name}", doctor.MISMATCH,
                            f"hook {hook_id} subscribes to {events or '(no events)'}, not {event!r}",
                            doctor.hooks_fix(loop, f"adds {event!r} to hook {hook_id}"))
    content_type = match["config"].get("content_type")
    if content_type != "json":
        return doctor.Check(f"hook:{name}", doctor.MISMATCH,
                            f"hook {hook_id} has content_type {content_type!r}, expected 'json'",
                            doctor.hooks_fix(loop, f"sets hook {hook_id}'s content_type to json: the gate reads a "
                                             "JSON payload, not form-encoded data"))
    delivery = check_deliveries(loop, hook_id, name)
    if isinstance(delivery, doctor.Check):
        return delivery
    delivery = f"{delivery}{latest_decision(loop, seat)}"
    if not match.get("active"):
        return doctor.Check(f"hook:{name}", doctor.VERIFIED,
                            f"hook {hook_id} → [webhook URL redacted] ({event}, PAUSED — nothing fires "
                            f"until `hermes dk arm --loop {loop['id']}`; {delivery})", paused=True)
    return doctor.Check(f"hook:{name}", doctor.VERIFIED,
                        f"hook {hook_id} → [webhook URL redacted] ({event}, active; {delivery})")


def latest_decision(loop: dict, seat: str) -> str:
    """The route's gate's latest recorded decision (#209), as a suffix for the hook line."""
    from .. import gate_decisions
    try:
        entry = gate_decisions.latest_per_gate(loop).get(f"gate_{seat}")
    except Exception:  # noqa: BLE001 - a diagnostic line never fails the check
        entry = None
    return f"; latest gate decision: {gate_decisions.line(entry)}" if entry else ""


# The gateway's answers to a delivery whose signature it would not accept: 401 is "Invalid
# signature", 403 a route that is disabled or has no HMAC secret to check against.
REJECTED = {401: "signature rejected — the hook's secret does not match the route's",
            403: "refused — the route is disabled or holds no secret"}


def check_deliveries(loop: dict, hook_id, name: str) -> "doctor.Check | str":
    """How the gateway answered this hook's most recent delivery — GitHub's only secret evidence.

    GitHub never returns a hook's secret, but it keeps each recent delivery with the status code
    the gateway answered. A latest delivery answered 401/403 is a hook signing with a secret the
    route does not hold (a previous install's, say): it looks armed and wakes nothing. Returns a
    failing/unknown ``Check``, or a short phrase for the verified line.
    """
    path = f"/repos/{loop['repo']}/hooks/{hook_id}/deliveries?per_page=30"
    data, error = gh.fetch(loop, path)
    if error or not isinstance(data, list) or not all(isinstance(d, dict) for d in data):
        reason = error or "no delivery list returned"
        return doctor.Check(f"hook:{name}", doctor.UNKNOWN,
                            f"hook {hook_id} found, but its recent deliveries could not be read ({reason}) "
                            "— whether its secret matches the route is unproven",
                            f"{config.reader_access_fix(loop, 'hooks')}, or look by hand: "
                            f"`gh api repos/{loop['repo']}/hooks/{hook_id}/deliveries`")
    stamped = [d for d in data if isinstance(d.get("delivered_at"), str)]
    if not stamped:
        return "no deliveries yet — the secret is unproven until the first one arrives"
    latest = max(stamped, key=lambda d: d["delivered_at"])
    code = latest.get("status_code")
    deliveries = f"`gh api repos/{loop['repo']}/hooks/{hook_id}/deliveries`"
    ping = f"`hermes dk selftest --loop {shlex.quote(loop['id'])} --no-model --ping`"
    if type(code) is not int or code <= 0:
        # GitHub recorded the delivery but no HTTP answer (a timeout, a refused connection): the
        # gateway never judged the signature, so nothing is proven — hook_ping refuses this too.
        return doctor.Check(f"hook:{name}", doctor.UNKNOWN,
                            f"hook {hook_id}'s latest delivery ({latest['delivered_at']}) got no HTTP "
                            f"response from the gateway (GitHub recorded: "
                            f"{latest.get('status') or 'no status'}) — it never answered, so whether "
                            "its secret matches is unproven",
                            f"check the gateway is running and reachable from GitHub (`hermes gateway "
                            f"status`), then {ping}; the delivery log: {deliveries}")
    if code in REJECTED:
        return doctor.Check(f"hook:{name}", doctor.MISMATCH,
                            f"hook {hook_id}'s latest delivery ({latest['delivered_at']}) got HTTP {code}: "
                            f"{REJECTED[code]}, so the hook wakes nothing",
                            f"`hermes dk uninstall --loop {shlex.quote(loop['id'])}` (deletes the "
                            f"hook), then re-run init --hooks so the new hook and route share one fresh "
                            f"secret; then `hermes dk arm --loop {shlex.quote(loop['id'])}`")
    if not 200 <= code < 300:
        # A 5xx is the gateway erroring on the delivery (and any other non-2xx is it refusing):
        # either way nothing woke, and the signature was never shown to verify.
        kind = "the gateway errored" if code >= 500 else "the gateway did not accept it"
        return doctor.Check(f"hook:{name}", doctor.MISMATCH,
                            f"hook {hook_id}'s latest delivery ({latest['delivered_at']}) got HTTP {code}: "
                            f"{kind}, so the hook wakes nothing and its secret is unproven",
                            f"read the gateway's log for that delivery ({deliveries}), fix what it "
                            f"reports, then {ping}")
    return f"latest delivery {code}"


# -- the report ------------------------------------------------------------------

_URL_IN_REPORT = re.compile(r"https?://[^\s`<>]+", re.IGNORECASE)


def _safe_report_text(text: str) -> str:
    """Never echo a configured URL: userinfo, path, query and fragment may all be secrets."""
    return _URL_IN_REPORT.sub("[webhook URL redacted]", text)
