"""Host-owned hook arming operation shared by CLI and Desktop (#645).

Terminal diagnostics remain private to the CLI. Desktop projections expose only fixed categories.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time

from . import config, doctor, gh

# A process restart invalidates outstanding confirmations; none are persisted or sent as config.
_CONFIRM_KEY = secrets.token_bytes(32)


def _common():
    # Lazy: cli re-exports checks, which itself imports this owner.
    from .cli import common
    return common


def _hook_write_fix(token_login: str | None, transient: bool = False,
                    loop: dict | None = None) -> str:
    """What to do when a hook read or write failed with the token ``arm`` used.

    ``transient`` is for failures that were neither a refusal nor a write that did not stick (a
    timeout, a 5xx): those are worth a retry before blaming the token.
    """
    if transient:
        return ("retry `arm`; if it fails again, check the repo's webhooks on GitHub — "
                + _hook_write_fix(token_login, loop=loop))
    return _common().hook_write_need(loop or {}, token_login)


def set_hooks(loop: dict, active: bool, token_login: str | None, *,
              hooks: list[dict] | None = None) -> tuple[list[str], bool]:
    """Flip this loop's repo hooks, then read each one back and report what GitHub now shows.

    Returns ``(lines, ok)``. ``ok`` is true only when every loop hook was observed in the asked-for
    state: a refused PATCH, a read-back that disagrees or cannot be made, an unreadable listing,
    a repo with no loop hooks, and a repo missing either seat's hook all make it false — ``arm``
    must never say "paused" about a hook GitHub still delivers to, nor "armed" about a loop that
    only one seat can hear.
    """
    names = _common()._routes_of(loop)
    seats = [(role, names[role]) for role in config.hook_roles(loop) if names.get(role)]
    wanted = tuple(name for _, name in seats)
    word = "active" if active else "paused"
    login = token_login or loop.get("read_token")
    try:
        if hooks is None:
            hooks = _common()._hook_listing(loop, token_login, require_active=False)
    except config.ConfigError as exc:
        return [f"could not read the repo's hooks: {exc}",
                f"fix: {_hook_write_fix(token_login, loop=loop)}"], False
    try:
        # One matcher (doctor.split_route_hooks). Arming credits a hook only at the registry's
        # route URL bound to the seat's profile: another profile's URL (the gateway answers it
        # 404), another path, a retired gateway — or a route named inside another route's name —
        # is not that seat's hook. Pausing asks the ownership question instead: every hook this
        # install made is stopped, even once `uninstall` has removed the route it posted to.
        own, foreign = doctor.split_route_hooks(loop, hooks, wanted, ownership=not active)
    except config.ConfigError as exc:
        return [f"cannot tell this loop's hooks from anyone else's: {exc}",
                f"fix: hermes dk set --loop {loop.get('id')} --host "
                "https://your-gateway.example"], False
    out, ok = [], True
    failed: list[tuple[int, list[str]]] = []      # (hook id, the errors that decide its fix)
    targets = {name: doctor.seat_route_target(loop, name) for name in wanted}
    role_of = {name: role for role, name in seats}
    for hook in foreign:
        name = doctor.hook_route_name(hook)
        want, reason = targets[name]
        if want is None and active:
            out.append(f"hook {hook['id']} posts to route {name!r}; {reason} — not armed, "
                       "left as it is")
            continue
        why = doctor.hook_url_difference(str(hook["config"].get("url") or ""),
                                         want or (doctor.install_hook_urls(loop, name) or [""])[0])
        out.append(f"hook {hook['id']} posts to route {name!r} at {why} — not this seat's hook "
                   "(nothing this loop serves receives it), left as it is")
    matched: set[str] = set()
    miswired: list[int] = []
    for hook in own:
        name = doctor.hook_route_name(hook)
        matched.add(name)
        if active:
            # At the seat's URL but subscribed to the wrong event (or not JSON), or with a
            # trailing slash the gateway 404s: doctor calls each a MISMATCH, so arming it would
            # report a loop live that the seat never hears. (Found by same_hook_url, judged by
            # exact_hook_url — pausing still stops it.)
            want = targets[name][0] or ""
            problem = _wake_problem(hook, want, role_of.get(name, ""))
            if problem:
                ok = False
                miswired.append(hook["id"])
                why = (problem if problem.endswith("never woken") else
                       f"{problem}, so it would not wake the {role_of.get(name, '?')} seat")
                out.append(f"hook {hook['id']} NOT armed: it {why} — left as it is")
                continue
        # Only a real bool is a state: a hook with no `active` (or a non-bool one) is flipped and
        # read back like any other, never taken as already there.
        if isinstance(hook.get("active"), bool) and hook["active"] == active:
            out.append(f"hook {hook['id']} already {word}")
            continue
        path = f"/repos/{loop['repo']}/hooks/{hook['id']}"
        _, error = gh.fetch(loop, path, method="PATCH", body={"active": active}, login=login)
        # A lost response is ambiguous and a stub or proxy can answer 200 without writing, so the
        # hook's state is whatever a fresh read says — never what was asked for.
        actual, read_error = gh.fetch(loop, path, login=login)
        if not isinstance(actual, dict) or not isinstance(actual.get("active"), bool):
            ok = False
            out.append(f"hook {hook['id']} NOT CONFIRMED {word}: "
                       + (f"PATCH failed ({error}); " if error else "")
                       + f"read-back failed ({read_error or 'no hook in the answer'})")
            failed.append((hook["id"], [e for e in (error, read_error) if e]))
            continue
        seen = "active" if actual["active"] else "paused"
        if actual["active"] == active:
            out.append(f"hook {hook['id']} → {seen} (read back)")
            continue
        ok = False
        out.append(f"hook {hook['id']} is still {seen}, not {word}"
                   + (f": PATCH failed ({error})" if error else ": GitHub accepted the PATCH "
                      "but the read-back disagrees"))
        # A PATCH GitHub accepted that did not stick is a refusal; a failed PATCH is judged by its
        # own code, so a timeout or a 5xx gets the retry advice, not the token-scope one.
        failed.append((hook["id"], [error or "refused"]))
    # Arming needs each seat's route bound in the registry; pausing only needs the hooks.
    unbound = [(role, name) for role, name in seats
               if active and targets[name][0] is None]
    if not matched and not unbound:
        return out + ["no loop hooks found at the routes' own URLs — run init --hooks first "
                      f"(looked for hooks posting to {', '.join(wanted) or 'a loop route'})"], False
    missing = [(role, name) for role, name in seats if name not in matched]
    for role, name in missing:
        reason = ((targets[name][1] if active else "") or
                  ("no repo hook posts to this route's URL, so the loop cannot be "
                   f"{'armed' if active else 'paused'} as a whole"))
        out.append(f"hook:{name} ABSENT ({role} seat) — {reason}")
    if miswired:
        out.append(f"fix: hook{'s' if len(miswired) > 1 else ''} {', '.join(map(str, miswired))}:"
                   f" `hermes dk doctor --loop {loop.get('id')}` names what each needs "
                   "(re-run init --hooks, or on GitHub set the event / content_type, or drop the "
                   f"URL's trailing slash — `hermes dk apply --hooks --loop {loop.get('id')}` "
                   "repoints a slashed hook), then run `arm` again")
    # The fix is per hook: one refused hook must not hide the retry advice another hook's 5xx
    # earned. Hooks that need the same fix share its line.
    advice: dict[str, list[int]] = {}
    for hook_id, errors in failed:
        # GitHub answers 404 to a token that may not see hooks, so 404 counts as a refusal too.
        refused = any(e == "refused" or any(f"HTTP {code}" in e for code in (401, 403, 404))
                      for e in errors)
        advice.setdefault(_hook_write_fix(token_login, transient=not refused, loop=loop),
                          []).append(hook_id)
    for text, ids in advice.items():
        out.append(f"fix: hook{'s' if len(ids) > 1 else ''} {', '.join(map(str, ids))}: {text}")
    if unbound:
        ok = False
        out.append(f"fix: `hermes dk doctor --loop {loop.get('id')}` names what each "
                   "route needs (its `route:` line and fix) — repair the route first, then run "
                   f"`arm{' --pause' if not active else ''}` again")
    if [pair for pair in missing if pair not in unbound]:
        ok = False
        out.append(f"fix: `hermes dk doctor --loop {loop.get('id')}` shows the hook each "
                   "seat needs; add the missing one (by hand with its route's URL and secret, or "
                   "via `init --hooks` for a loop being set up — hook write on "
                   f"{loop.get('repo')}, --admin-token <login>), then run "
                   f"`arm{' --pause' if not active else ''}` again")
    return out, ok


def perform(loop: dict, active: bool, token_login: str | None = None, *,
            hooks: list[dict] | None = None) -> tuple[list[str], bool]:
    """The CLI's complete host action: switch, verify, and ping only a confirmed resume."""
    from . import config
    if config.app_ingress(loop):
        # #1239: no repo hooks; the App's webhook cannot be paused per repository, so the loop's
        # own flag is what the App's dispatcher reads before any gate runs.
        from . import app_ingress, routes, state as state_mod
        route = config.app_route(loop)
        if active and not routes.route(route):
            # Armed would be a lie: the gateway 404s every delivery until the route exists.
            return [f"not armed: the App's route {route} is not installed, so nothing reaches "
                    f"the gates (`hermes dk doctor --loop {loop['id']} --repair` restores it)"], False
        st = state_mod.state_for(loop)
        try:
            app_ingress.set_paused(st, not active)
        except OSError as exc:
            return [f"the pause flag could not be written ({exc}): nothing changed"], False
        done = app_ingress.paused(st) is (not active)
        return [(f"{'armed' if active else 'paused'}: events arrive through "
                 f"{loop.get('ingress_app')}'s webhook, so its deliveries for this loop are "
                 f"{'acted on' if active else 'dropped before any gate runs'} (read back)")
                if done else "the pause flag did not read back: state unknown"], done
    lines, ok = set_hooks(loop, active, token_login, hooks=hooks)
    if ok and active:
        ok = _common()._ping_loop_hooks(loop, token_login)
    return lines, ok


class StaleConfirmation(ValueError):
    """The confirmed target or its observed state no longer matches."""


def _identity(loop: dict, hooks: list[dict], targets: dict) -> str:
    # HMAC, not a serialized config or an unkeyed digest: paths, URLs and credentials never
    # leave the host, nor can a client construct a confirmation from guessed configuration.
    # Ignore delivery counters/last_response/updated_at: an incoming delivery is not a
    # configuration change and must not invalidate a confirmation while the loop is active.
    identities = [{'id': h['id'], 'active': h.get('active'), 'events': h.get('events'),
                   'config': {key: h['config'].get(key) for key in
                              ('url', 'content_type', 'insecure_ssl')}}
                  for h in sorted(hooks, key=lambda h: h['id'])]
    body = json.dumps([loop, identities, targets],
                      sort_keys=True, separators=(',', ':'), default=str).encode()
    return hmac.new(_CONFIRM_KEY, body, hashlib.sha256).hexdigest()


def plan(loop: dict, token_login: str | None = None) -> dict:
    """Read actual owned hooks; mixed/missing/unreadable is unknown, never a guessed pause.

    The private snapshot is consumed by confirmed_perform, never returned to the UI. Paused
    means every owned hook was observed inactive; armed means every owned hook is active,
    not that its wiring is healthy. Pause needs verified ownership and complete coverage;
    resume additionally needs every required seat's correctly wired hook. Nothing is created,
    installed, drained or reconfigured here.
    """
    result = {'loop': loop['id'], 'repo': loop['repo'], 'state': 'unknown',
              'eligible': False, 'confirmation': None,
              'reason': 'Hook state could not be verified.', 'read_at': time.time()}
    from . import config
    if config.app_ingress(loop):
        # #1239: no repo hooks to snapshot; the pause flag is switched on the host (its change is
        # not one Desktop's hook-bound confirmation can verify).
        from . import app_ingress, state as state_mod
        paused = app_ingress.paused(state_mod.state_for(loop))
        result.update(state='paused' if paused else 'armed',
                      reason=("Events arrive through the App's webhook; pause or resume it on the "
                              f"host: `hermes dk arm --loop {loop['id']}{'' if paused else ' --pause'}`."))
        return result
    try:
        hooks = _common()._hook_listing(loop, token_login, require_active=False)
        names = _common()._routes_of(loop)
        seats = {names[role]: role for role in config.hook_roles(loop) if names.get(role)}
        wanted = tuple(seats)
        own, _ = doctor.split_route_hooks(loop, hooks, wanted, ownership=True)
        exact, _ = doctor.split_route_hooks(loop, hooks, wanted, ownership=False)
        targets = {name: doctor.seat_route_target(loop, name) for name in wanted}
        if not own or not seats or any(
                type(h.get('active')) is not bool or type(h.get('id')) is not int or
                not isinstance(h.get('events'), list) or
                any(not isinstance(event, str) for event in h['events']) for h in own):
            return result
        matched = {doctor.hook_route_name(h) for h in own}
        if not set(seats).issubset(matched):
            result['reason'] = 'Required loop hooks are missing.'
            return result
        # Resume is safe only if the same host operation would accept every owned hook.
        resume_ready = (len(exact) == len(own) and
                        all(targets[name][0] is not None for name in seats) and
                        all(not _wake_problem(h, targets[doctor.hook_route_name(h)][0] or '',
                                              seats[doctor.hook_route_name(h)]) for h in own))
        if all(h['active'] is False for h in own):
            result['state'] = 'paused'
            result['eligible'] = resume_ready
            result['reason'] = ('All loop hooks are paused.' if resume_ready else
                                'Hooks are paused; repair hook wiring before resuming.')
        elif all(h['active'] is True for h in own):
            # Stopping delivery needs ownership, not wake readiness. Keep the wiring warning:
            # active does not establish that any seat actually receives the intended events.
            result.update(state='armed', eligible=True,
                          reason=('All loop hooks are armed.' if resume_ready else
                                  'All loop hooks are active; repair hook wiring before resuming.'))
        else:
            result['reason'] = 'Loop hooks are mixed or not correctly wired; inspect them first.'
        if result['eligible']:
            result['confirmation'] = _identity(loop, own, targets)
            result['_hooks'] = hooks
    except Exception:
        # Terminal exceptions may include PAT paths, URLs or GitHub transport bodies.
        result.update(state='unknown', eligible=False, confirmation=None,
                      reason='Hook state could not be verified.')
        result.pop('_hooks', None)
    finally:
        result['read_at'] = time.time()
    return result


def public_plan(plan: dict) -> dict:
    return {key: value for key, value in plan.items() if not key.startswith('_')}


def confirmed_perform(loop: dict, action: str, confirmation: str) -> dict:
    """Re-read identity before writing; always project observed state after any partial action."""
    before = plan(loop)
    expected = before['confirmation']
    if (not before['eligible'] or not expected or
            not hmac.compare_digest(expected, confirmation) or
            action != ('pause' if before['state'] == 'armed' else 'resume')):
        raise StaleConfirmation('Hook confirmation is stale or unavailable; read hook state again.')
    ok = False
    try:
        _, ok = perform(loop, action == 'resume', hooks=before['_hooks'])
    except Exception:
        # A failure can follow a write. Still re-read; never claim rollback or success.
        pass
    after = public_plan(plan(loop))
    desired = 'paused' if action == 'pause' else 'armed'
    ok = bool(ok and after['state'] == desired)
    # Once a write/read-back could not be confirmed, even a subsequent listing is not enough
    # to offer another action automatically. Reconciliation requires a fresh explicit read.
    if not ok:
        after.update(eligible=False, confirmation=None)
    after.update(ok=ok, answer=(f'Loop hooks confirmed {desired}.' if ok else
                               'Hook action was not fully confirmed; inspect hook state before retrying.'))
    return after


def _wake_problem(hook: dict, wanted_url: str, role: str) -> str:
    return (('' if doctor.exact_hook_url(str(hook['config'].get('url') or ''), wanted_url)
             else doctor.SLASH_404) or doctor.hook_wake_problem(hook, role))


