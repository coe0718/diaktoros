"""``hermes dk apply``: push the settings form onto a loop (#1349, split from ``settings``).
"""
from __future__ import annotations

import pathlib
import shlex

from .. import (
    ci,
    config,
    doctor,
    gate_shims,
    observer,
    protection,
    route_intent,
    routes,
    state as state_mod,
    util,
)
from . import common as cli_common
from . import route_sync
from ..config import credentials
from ..config import settings_form
from .common import GATE_SCRIPT


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
        rc = max(rc, route_sync._ensure_hooks(loop, getattr(args, "admin_token", "") or None, args.dry_run))
    return rc


# Settings-form keys that live inside a loop section, which ``_apply`` compares as it does the
# top-level ones (triage.fix_daily_turns is compared as the issue fixer's daily turns).
_NESTED_FORM_KEYS = (("triage", "auto_fix_labels"), ("triage", "auto_fix_daily"),
                     ("observer", "weekly_report"))


def _role_login(loop: dict, role: str) -> str:
    return (config.adjudicator_login(loop) if role == "adjudicator"
            else config.seat_login(loop, role))


def _app_reverts(before: dict, after: dict) -> list[tuple[str, str, str]]:
    """``(role, App login, form login)`` for each seat the form would move off a GitHub App."""
    out = []
    for role in (*config.SEAT_KEYS, "adjudicator"):
        old, new = _role_login(before, role), _role_login(after, role)
        if old and new and old.lower() != new.lower() and cli_common._app_ref(before, old):
            out.append((role, old, new))
    return out


def _app_revert_notes(loop: dict, updated: dict, dry_run: bool) -> None:
    """What ``--identity-from-form`` leaves behind when it moves a seat off its App (#1307).

    Called only once the move is certain: from the dry run's report (phrased as what *would*
    happen) and after the config is written, never before a refusal, a preflight or a rollback
    (#1979 review, F2). As with ``identity``, the App's ``apps`` entry and its login on the
    allowlist stay, so open PRs keep their verdicts and authorship; ``doctor`` still checks that
    key until it is unmapped. A reviewer moved off its App breaks a ``protect`` ruleset pinned to
    that App whatever ``merge_check`` says (#1979 review, F1): see ``_merge_check_note``."""
    reverts = _app_reverts(loop, updated)
    for role, old, new in reverts:
        allow = {"reviewer": "reviewers", "fixer": "fixers"}.get(role)
        kept = f"its 'apps' entry{' and its login on ' + repr(allow) if allow else ''}"
        moves, stay = ("would move", "would stay") if dry_run else ("moved", "stay")
        print(f"  --identity-from-form: the {role} {moves} from the GitHub App {old!r} to {new!r}; "
              f"{kept} {stay} (doctor still checks its key) — remove them once no open PR needs "
              "them")
    if any(role == "reviewer" for role, _, _ in reverts):
        _merge_check_note(loop, updated, dry_run)


def _merge_check_state(loop: dict, updated: dict, dry_run: bool) -> str:
    """How ``diaktoros/review`` is posted after the reviewer's move: on or off before and after
    (``config.merge_check_on``, the predicate the host posts by), and as whose check — the new
    App's check run or the PAT reviewer's commit status (#1979 review, F1 and F3)."""
    was, now = config.merge_check_on(loop), config.merge_check_on(updated)
    if not now:
        if was:
            return (("would stop being posted" if dry_run else "is now off")
                    + " (merge_check: auto follows an App reviewer)")
        return "would stay off" if dry_run else "stays off"
    new_id = protection.reviewer_app_id(updated)
    who = (f"a check run of the App {updated.get('reviewer_seat')!r} ({new_id})" if new_id
           else "the PAT reviewer's commit status")
    if was:
        return f"would stay on, posted as {who}" if dry_run else f"stays on, posted now as {who}"
    return f"would turn on, posted as {who}" if dry_run else f"is now on, posted as {who}"


def _merge_check_note(loop: dict, updated: dict, dry_run: bool) -> None:
    """The merge check after the reviewer left the App it was pinned to (#1307, #1979 F1, F3).

    Printed for every reviewer move off an App, whatever ``merge_check`` was before: a ruleset
    ``protect`` wrote while the reviewer was that App stays pinned to its id after
    ``set --merge-check off`` (nothing removes it), so turning the check on later meets the same
    pin. What a rewritten ruleset accepts depends on where the reviewer went:
    ``protection.ruleset_body`` re-pins it to a new App, and drops the pin for a PAT's commit
    status (which cannot be pinned). Either way out goes through ``protect``: delete the ruleset,
    or rewrite it from the loop as it now is."""
    lid, old_id = loop["id"], protection.reviewer_app_id(loop)
    new_id = protection.reviewer_app_id(updated)
    if old_id is not None and new_id == old_id:
        return                                        # another login on the same App: pin holds
    protect = f"hermes dk protect --loop {lid} --admin-token LOGIN"
    old = f"the App {old_id}" if old_id is not None else "the old reviewer App"
    if config.merge_check_on(updated):
        rewrite = f"rewrite it with `{protect}`"
    else:
        rewrite = (f"keep the check: `hermes dk set --loop {lid} --merge-check on`, then rewrite "
                   f"the ruleset with `{protect}`")
    if new_id:
        who, result = (f"the new reviewer App {new_id}'s check run does not satisfy",
                       f"that re-pins {ci.MERGE_CHECK} to the App {new_id}")
    else:
        who, result = ("the PAT reviewer can never satisfy",
                       f"that drops the App pin and accepts {ci.MERGE_CHECK} from any source (a "
                       "commit status cannot be pinned), so anyone with write access can post it")
    print(f"  the {ci.MERGE_CHECK} merge check {_merge_check_state(loop, updated, dry_run)}. If "
          f"`protect` required it while the reviewer was that App (turning merge_check off since "
          f"leaves the ruleset in place), its ruleset is pinned to {old}, which {who}: every PR "
          f"stays blocked until you either delete it (`{protect} --remove`) or {rewrite} — "
          f"{result}")


def _apply(args) -> int:
    """Make a loop match the plugin settings — one push, with the diff printed.

    Push, not subscription: a running loop whose numbers changed under it is exactly the kind of
    thing nobody can debug at 2am. The same validation as ``init``/``set`` still applies, so a
    settings form asking for two reviews at once without a clone path (and without the isolated
    worker's runtime file) is refused here too.

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
        # Select before stat: stale PAT fields for App seats are not effective paths (#1197).
        credentials.verify_token_settings(cli_common._SETTINGS, loop=loop)
        updated = config.normalize(settings_form.apply_settings(loop, cli_common._SETTINGS))
    except config.ConfigError as exc:
        print(f"settings refused: {exc}")
        return 2

    if _identity_refusal(args, loop, updated):
        return 2

    _stale_pat_notes(updated)

    identity, touched = route_sync._seat_diffs(loop, updated)
    # The installed registry can drift independently of the loop and the form. Repair those
    # routes through the same ownership, seat and in-flight preflight as an identity push.
    binds = route_sync._route_binds(updated, set(cli_common._routes_of(updated)))
    # A route installed by an older release (the pre-#21 breach route on gate_reviewer.py) is
    # repaired here too: `init` refuses an existing loop, so apply is the only reconcile path.
    repairs = route_sync._stale_scripts(updated)
    drifted = route_sync._drifted_routes(updated)
    rebinding = touched | set(binds) | set(repairs) | set(drifted)
    try:
        shim_pairs, shim_lines = _verify_write(loop, updated, rebinding)
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
        rc, leftover_hooks = route_sync._recreate_routes(updated, missing, dry_run=args.dry_run,
                                              token_login=getattr(args, "admin_token", "") or None)
        if rc:
            return rc

    changes = _form_changes(loop, updated)

    missing_routes = sorted(name for role, name in cli_common._routes_of(updated).items()
                            if role in touched and not routes.route(name))

    if not changes and not identity and not binds and not repairs and not drifted:
        return _already_matches(args, loop, updated, leftover_hooks)
    if _host_refusal(args, loop, updated):
        return 2
    _print_plan(changes, identity, binds, drifted, repairs, missing_routes)
    if missed := [role for role, name in cli_common._routes_of(updated).items()
                  if role in touched and name in missing_routes]:
        print(f"refused: {', '.join(missed)} would move to a route that does not exist yet")
        return 2

    if args.dry_run:
        return _dry_run_report(args, loop, updated, binds, repairs, drifted, missing,
                               leftover_hooks)

    if _busy_refusal(args, loop, rebinding):
        return 2

    # Preflight remote hooks before any local mutation. Snapshot each owned route and roll back
    # both surfaces on any failure; config is published only after route/hook readback agrees.
    try:
        hook_moves, redundant_hooks = route_sync._hook_moves(route_sync._hook_origin(loop, drifted), updated, binds,
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
        _check_readback(updated, rebound)
        for hook_id, old, new, ssl in hook_moves:
            attempted_hooks.append((hook_id, old, ssl))
            cli_common._patch_hook_url(loop, hook_id, new, getattr(args, "admin_token", "") or None,
                            insecure_ssl=ssl)
        path = cli_common._write_config(updated) if changes or identity else config_path
        if rebound:
            route_intent.record_live(updated, [name for _, name in rebound])
    except Exception as exc:
        return _rollback(args, loop, exc, attempted_hooks, previous, config_path,
                         previous_config)
    _report_written(loop, updated, path, rebound, repairs, drifted, hook_moves, redundant_hooks)
    return 1 if route_sync._diverged(updated) or redundant_hooks or leftover_hooks else 0


def _report_written(loop: dict, updated: dict, path, rebound: list, repairs: dict, drifted: dict,
                    hook_moves: list, redundant_hooks: list) -> None:
    """What a successful apply wrote: the config, each rebound route and hook, and what
    ``--identity-from-form`` left behind — printed only now, once the write has landed."""
    print(f"loop config updated: {path}")
    for role, name in rebound:
        print(f"  route {name} rebound → profile {config.seat_profile(updated, role)}"
              + (f", script {GATE_SCRIPT[role]}" if role in repairs else "")
              + (f", {', '.join(drifted[role][1])} restored" if role in drifted else ""))
    for hook_id, _, new, _ssl in hook_moves:
        print(f"  hook {hook_id} → {new}")
    for hook, keep in redundant_hooks:
        print(f"  {route_sync._redundant_hook_line(loop, hook, keep)}")
    _app_revert_notes(loop, updated, dry_run=False)





def _verify_write(loop: dict, updated: dict, rebinding: set) -> tuple:
    """Raise ``ConfigError`` unless what this apply would write can run; the gate shims' pairs
    and the lines a dry install of them prints."""
    # Validate what this apply would *write*: a loop that predates the seat checks keeps
    # loading, but a seat this push moves must be one that can actually run.
    credentials.verify_seats(updated, rebinding)
    adj = config.adjudicator_login(updated)
    if adj and (adj != config.adjudicator_login(loop)
                or cli_common._credential_label(loop, adj) != cli_common._credential_label(updated, adj)):
        credentials.verify_adjudicator_token(updated)
        credentials.verify_credentials(updated)
    if rebinding:
        cli_common._verify_routes(updated, rebinding)
    # Refuses a foreign file (#105) — only over what this apply writes: the config's pairs. A
    # route the registry holds elsewhere is what apply is about to rebind away, so it never
    # blocks; disagreements left afterwards are reported by ``_diverged``.
    shim_pairs = gate_shims.wanted(updated)
    shim_lines = gate_shims.install(updated, dry_run=True, report=False, pairs=shim_pairs)
    return shim_pairs, shim_lines


def _busy_refusal(args, loop: dict, rebinding: set) -> bool:
    """Print the seats in flight on a route this apply rebinds, and refuse (True) unless
    ``--while-busy`` says to go ahead anyway."""
    busy = cli_common._busy_seats(loop, rebinding) if rebinding else []
    if busy and not getattr(args, "while_busy", False):
        for line in busy:
            print(f"  {line}")
        print("refused: a seat is in flight, and its live run holds the old profile and login "
              "until it ends. Let it finish, then apply again — or pass --while-busy to rebind "
              "now, knowing that run still finishes under the identity it started with.")
        return True
    for line in busy:
        print(f"  {line}  (--while-busy: that run keeps the identity it started with)")
    return False

def _stale_pat_notes(updated: dict) -> None:
    """A note for each form token file this apply leaves unapplied: its login is an App identity."""
    # A form token file for a login the loop gives a GitHub App identity is not applied (the
    # identity keeps its App, #1163): say so, rather than "matches" over it.
    form = settings_form.settings_defaults(cli_common._SETTINGS)
    for role, key in config.TOKEN_FILE_SETTINGS.items():
        login = (config.adjudicator_login(updated) if role == "adjudicator"
                 else config.seat_login(updated, role))
        if str(form.get(key) or "").strip() and cli_common._app_ref(updated, login):
            print(f"[{updated['id']}] note: the plugin settings' {key} is not applied — {login} "
                  "is a GitHub App identity on this loop (stale PAT setting; App selected)")


def _host_refusal(args, loop: dict, updated: dict) -> bool:
    """Print why the host may not change yet, and say so (True): unsettled observer notices."""
    if not args.dry_run and updated.get("host") != loop.get("host") and loop.get("observer"):
        try:
            outstanding = observer.unsettled(state_mod.state_for(loop))
        except (OSError, ValueError) as exc:
            print(f"observer ledger cannot be checked — host unchanged: {exc}")
            return True
        if outstanding:
            print(f"refused: {outstanding} observer notice(s) are unsettled; retry/resolve "
                  "them before changing the host")
            return True
    return False


def _print_plan(changes: list, identity: list, binds: dict, drifted: dict, repairs: dict,
                missing_routes: list) -> None:
    """What this apply moves: each setting and identity, then each route it rewrites or cannot."""
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


def _check_readback(updated: dict, rebound: list) -> None:
    """Raise ``ConfigError`` unless every rebound route reads back as the config asks."""
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

def _identity_refusal(args, loop: dict, updated: dict) -> bool:
    """Print why ``updated`` may not be applied for its identities, and say so (True): a seat the
    form would move off its GitHub App (#1296), or a reader the four-identity rule or the
    credentials refuse."""
    # #1296: a seat moved to a GitHub App (`identity`) is not moved back by a form that still names
    # the old login: that would silently drop the App, and with a reviewer App the merge check.
    reverts = _app_reverts(loop, updated)
    if reverts and not getattr(args, "identity_from_form", False):
        for role, old, new in reverts:
            print(f"settings refused: {loop['id']}: the plugin settings name {new!r} for the {role}, "
                  f"but this loop's {role} is the GitHub App {old!r} (moved with `hermes dk "
                  "identity`); apply would move it back")
        print("fix: put the App's login in the settings form (or blank that login field), or pass "
              "--identity-from-form to move the identity back to what the form names")
        return True

    # The four-identity rule holds for the config apply would leave behind, whether or not this
    # push moves an identity: "nothing changed" must not endorse a reader that is also a seat.
    problem = credentials.reader_problem(updated)
    if problem:
        print(f"settings refused: {updated['id']}: {problem} — {config.FOUR_IDENTITY_RULE}")
        print(f"fix: {credentials.reader_fix(updated)}")
        return True
    reader = str(updated.get("read_token") or "")
    if util.login_entry(config.credentials_map(updated), reader) is None:
        # doctor fails this loop; apply must not report success over it either. A PAT or a
        # GitHub App identity (#1163) both count.
        print(f"settings refused: {updated['id']}: read_token {reader!r} has no entry in "
              "'tokens' or 'apps' — the gates read GitHub as that login and have no credential "
              "to read it with")
        print(f"fix: {credentials.reader_fix(updated, reader)}")
        return True
    return False


def _form_changes(loop: dict, updated: dict) -> list:
    """``(name, was, now)`` for each setting the form moves, identities aside."""
    changes = []
    for key in ("cap", "base", "host", "grace_min", "ttl_min", "inflight_ttl_min",
                "turn_budget_s", "attribution", "fixer_check", "review_after_ci", "merge_check",
                "ingress", "ingress_app",
                "fix_ci", "ci_webhook", "comment_webhook", "fixer_scope_lock", "adjudicator_approves", "adjudicator_file_issues", "change_proof", "change_proof_max", "ci_fix_cap", "review_only_update", "required_checks", "human_paths",
                "generated", "ci_coverage", "prices", "review_only", "operator_agents", "review_only_cap",
                "review_only_daily", "shadow"):
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
    for seat in ("reviewer", "fixer", "shadow", "triage"):
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
    return changes


def _already_matches(args, loop: dict, updated: dict, leftover_hooks: int) -> int:
    """Nothing in the loop config moves: say whether the routes and repo hooks agree with it, and
    repoint a hook whose URL differs only by a trailing slash."""
    # Never "matches" over a route the registry holds differently (#112 review): the loop
    # config can agree with the settings while the gateway serves something else.
    left = route_sync._diverged(updated, header=f"[{loop['id']}] the loop config matches the plugin "
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
            slashed, duplicates = route_sync._hook_moves(loop, updated, {}, token, check_only=True)
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
        print(f"  {route_sync._redundant_hook_line(loop, hook, keep)}")
    return 1 if left or leftover_hooks or duplicates else 0


def _dry_run_report(args, loop: dict, updated: dict, binds: dict, repairs: dict, drifted: dict,
                    missing: dict, leftover_hooks: int) -> int:
    """``--dry-run``'s end: what would still disagree afterwards, and the hook moves (reads only)."""
    rewritten = ({bind[0] for bind in binds.values()} | {n for n, _ in repairs.values()}
                 | {n for n, _ in drifted.values()})
    if getattr(args, "recreate_routes", False):
        rewritten |= set(missing.values())
    left = route_sync._diverged(updated, rewritten=rewritten)
    try:
        hook_before = route_sync._hook_origin(loop, drifted)
        if hook_before is not loop:
            # The routes' origin moves, so their hooks move too: preview it (reads only).
            moves, redundant = route_sync._hook_moves(hook_before, updated, binds,
                                           getattr(args, "admin_token", "") or None)
            for hook_id, _old, new, _ssl in moves:
                print(f"  hook {hook_id} would move → {new}")
            for hook, keep in redundant:
                print(f"  {route_sync._redundant_hook_line(loop, hook, keep)}")
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    _app_revert_notes(loop, updated, dry_run=True)
    print("(dry run — nothing written: no loop config, no routes touched)")
    if left:
        print("  apply would exit 1: the route(s) above still disagree with the config after it")
    return 1 if left or leftover_hooks else 0


def _rollback(args, loop: dict, exc: Exception, attempted_hooks: list, previous: dict,
              config_path: pathlib.Path, previous_config: bytes) -> int:
    """A reconciliation failed part-way: put back the hook URLs, routes and config it touched, and
    say which of them could not be restored."""
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

def add_apply(sub) -> None:  # noqa: ANN001
    """The ``apply`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    apply_cmd = sub.add_parser("apply", help="Push the plugin settings onto a loop")
    apply_cmd.add_argument("--loop", required=True,
                           help="loop id (its config file name; `list` shows them)")
    apply_cmd.add_argument("--dry-run", action="store_true",
                           help="show the diff without writing it")
    apply_cmd.add_argument("--identity-from-form", action="store_true",
                           help="move a seat off its GitHub App identity to the login the settings "
                                "form names (refused otherwise: `identity` moves seats deliberately)")
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
