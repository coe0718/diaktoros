"""``hermes dk identity`` and ``hermes dk fixer-push`` (#1349, split from ``settings``).
"""
from __future__ import annotations

import json
import pathlib

from .. import (
    config,
)
from . import common as cli_common
from ..config import settings_form


def cmd_fixer_push(args) -> int:
    """Change only this repository's unattended push permission by explicit operator action."""
    try:
        with config.push_policy_lock():
            return _cmd_fixer_push_locked(args)
    except (OSError, ValueError, config.ConfigError) as exc:
        print(f"fixer push policy update not confirmed: {exc}")
        return 2


def _cmd_fixer_push_locked(args) -> int:
    try:
        loop = config.load_id(args.loop)
        config.by_repo(loop['repo'])  # duplicate owners fail closed
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    if args.enable and not args.acknowledge_pr_race:
        print("refused: host-operator policy only (not verified GitHub owner/admin consent). "
              "Unattended fixer pushes have a residual PR-metadata/ref race: "
              "closing, drafting or retargeting a PR between the last check and Git's "
              "exact-SHA-lease push can still publish. Inspect the production boundary and "
              "pass --acknowledge-pr-race to opt in for this repository.")
        return 2
    if args.enable:
        try:
            busy = cli_common._busy_seats(loop, {"fixer"})
            from ..run_supervisor import Supervisor, ACTIVE
            ledger = config.host_path("ledger")
            if ledger.exists():
                with Supervisor(ledger)._connect() as con:
                    rows = con.execute(
                        'SELECT pr,state FROM runs WHERE repo=? AND seat=? '
                        'AND state IN (?,?,?,?) LIMIT 1',
                        (loop['repo'], 'fixer', *ACTIVE)).fetchall()
                busy.extend(f"fixer supervisor on PR {row['pr']} ({row['state']})"
                            for row in rows)
        except (OSError, ValueError) as exc:
            print(f"refused: cannot check active fixer runs: {exc}")
            return 2
        if busy:
            print("refused: fixer run in flight; wait for it to finish before arming pushes: "
                  + "; ".join(busy))
            return 2
    enabled = bool(args.enable)
    if config.unattended_fixer_push_enabled(loop) == enabled:
        print(f"[{loop['id']}] unattended fixer push already {'enabled' if enabled else 'disabled'}")
        return 0
    updated = config.normalize({**loop, "unattended_fixer_push": enabled})
    if args.dry_run:
        print(f"[{loop['id']}] dry run — would {'enable' if enabled else 'disable'} "
              "unattended fixer pushes; nothing written")
        return 0
    try:
        path = cli_common._write_config_locked(updated, policy_change=True)
        actual = config.load_id(loop["id"])
        if actual["repo"] != loop["repo"] or config.unattended_fixer_push_enabled(actual) != enabled:
            raise config.ConfigError("readback does not match the requested repository/policy")
    except (OSError, ValueError, config.ConfigError) as exc:
        print(f"fixer push policy update not confirmed: {exc}")
        return 2
    print(f"[{loop['id']}] {loop['repo']}: host-operator unattended fixer push "
          f"{'enabled (not GitHub owner consent; residual PR-metadata/ref race acknowledged)' if enabled else 'disabled'} "
          f"in {path}")
    if enabled:
        try:
            from .. import state as state_mod
            held = [key for key, entry in state_mod.state_for(actual).queue_items("fixer").items()
                    if config.is_fixer_push_hold(entry)]
        except Exception:
            held = []
        if held:
            print(f"  {len(held)} held verdict(s) ({', '.join(sorted(held))}) start on the next "
                  f"watchdog sweep, or now: `hermes dk drain --loop {loop['id']} "
                  "--seat fixer`")
    return 0


# -- the parser for this module's commands (#676 step 7) --------------------------------------


# The ledger seats whose runs write as each identity. Triage writes as the reviewer's login
# unless it has its own (config.triage_login), so it is added for the reviewer when it does.
# The reader reads for every run, shadows included, so any run in flight blocks moving it.
_IDENTITY_SEATS = {"reviewer": ("reviewer",), "fixer": ("fixer", "issue_fixer"),
                   "adjudicator": ("adjudicator",)}


def _identity_runs(loop: dict, seat: str) -> tuple[str, ...] | None:
    """The ledger seats a switch of ``seat`` waits for; None for every seat (the reader)."""
    if seat == "read":
        return None
    seats = _IDENTITY_SEATS[seat]
    if seat == "reviewer" and config.triage_login(loop) == config.seat_login(loop, "reviewer").lower():
        seats += ("triage",)
    return seats


def cmd_identity(args) -> int:
    """Move one identity of a loop to a GitHub App, or back to a PAT (#1163 step 6).

    The App is proven before anything is written: its key file, then its live bot user (an
    installation token minted for this repository, so an App not installed on it is refused).
    The old login stays on the allowlist, so open PRs keep the verdicts and authorship it earned,
    and its PAT mapping stays until the operator removes it. Refused while that identity has a
    run in flight. A seat's login is not part of its route, so no route or hook changes."""
    from .. import gh, run_supervisor
    from ..config import credentials
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    lid = loop["id"]
    if bool(args.app) == bool(args.pat):
        print("refused: give one of --app LOGIN=APP_ID:/abs/key.pem or --pat LOGIN=/abs/path")
        return 2
    if args.app:
        split = cli_common._split_app_pair(args.app)
        if split is None:
            return 2
        login, spec = split
    else:
        split = cli_common._split_token_pair(args.pat, "--pat")
        if split is None:
            return 2
        login, spec = split
        try:
            credentials.check_token_file(spec, f"--pat {login}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        spec = str(pathlib.Path(spec.strip()).expanduser())
    seat = args.seat
    if seat == "adjudicator" and not (loop.get("adjudicator") or {}).get("route"):
        print("refused: this loop has no adjudicator route, so the adjudicator posts nothing — turn "
              f"adjudication on first (`hermes dk set --loop {lid} --adjudicator-profile PROFILE`)")
        return 2
    old = (str(loop.get("read_token") or "") if seat == "read" else
           config.adjudicator_login(loop) if seat == "adjudicator" else config.seat_login(loop, seat))
    # Refuse while this identity is acting: a run would finish as the old login on a new mapping.
    try:
        db = run_supervisor.production_ledger()
        busy = uncertain = 0
        if db.exists():
            seats = _identity_runs(loop, seat)
            which = "" if seats is None else f" AND seat IN ({','.join('?' * len(seats))})"
            from .. import ledger
            con = ledger.read_only(db)
            try:
                rows = con.execute(
                    f"SELECT state FROM runs WHERE repo=?{which}"
                    f" AND state IN ({','.join('?' * len(run_supervisor.ACTIVE))})",
                    (loop["repo"], *(seats or ()), *run_supervisor.ACTIVE)).fetchall()
            finally:
                con.close()
            busy = len(rows)
            uncertain = sum(1 for (state,) in rows if state == "uncertain")
    except Exception as exc:
        print(f"refused: the run ledger cannot be read, so a run in flight cannot be ruled out "
              f"({type(exc).__name__}: {exc})")
        return 2
    if busy:
        # An uncertain run never settles by itself: only `retry` moves it.
        settle = (f" ({uncertain} uncertain: settle each with `hermes dk retry --loop {lid} --pr N`, "
                  "`hermes dk explain` names them)" if uncertain else "")
        print(f"refused: {busy} run(s) acting as the {seat} identity are in flight on {lid}; "
              f"switch once they finish{settle}")
        return 2
    raw = json.loads(json.dumps(loop))
    tokens = {k: v for k, v in (raw.get("tokens") or {}).items() if k.lower() != login.lower()}
    apps = {k: v for k, v in (raw.get("apps") or {}).items() if k.lower() != login.lower()}
    if args.app:
        apps[login] = spec
    else:
        tokens[login] = spec
    raw["tokens"], raw["apps"] = tokens, apps
    seats_cfg = {k: dict(v or {}) for k, v in (raw.get("seats") or {}).items()}
    if seat == "read":
        raw["read_token"] = login
    else:
        seats_cfg.setdefault(seat, {})["login"] = login
        if seat == "reviewer":
            raw["reviewer_seat"] = login
        allow = {"reviewer": "reviewers", "fixer": "fixers"}.get(seat)
        if allow:
            # The old login stays: open PRs keep the verdicts and authorship it earned.
            current = list(raw.get(allow) or [])
            if login.lower() not in {str(x).lower() for x in current}:
                raw[allow] = current + [login]
    raw["seats"] = seats_cfg
    try:
        updated = config.normalize(raw)
        # The checks every other identity write runs: the reader and the seats stay distinct
        # accounts (a shared one leaves the broker denying every write), each mapped and private.
        credentials.verify_credentials(updated, {"read", seat} if seat != "read" else {"read"})
        credentials.verify_seats(updated, {seat} & set(config.SEAT_KEYS))
        account = gh.principal(updated, login)
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    except Exception as exc:
        print(f"refused: {login!r} could not be resolved on GitHub ({type(exc).__name__}: {exc}) — "
              "nothing written")
        return 2
    if not isinstance(account, dict) or str(account.get("login") or "").lower() != login.lower():
        print(f"refused: {login!r}'s credential does not resolve to that login on GitHub "
              f"({'an App not installed on ' + loop['repo'] + '?' if args.app else 'wrong PAT?'}) "
              "— nothing written")
        return 2
    kind = (f"App {spec['app_id']}, key {spec['key_file']}" if args.app else f"PAT {spec}")
    if args.dry_run:
        print(f"[{lid}] dry run: {seat} {old or '(none)'} → {login} ({kind}); resolved on GitHub as "
              f"{account.get('login')} (id {account.get('id')}); nothing written")
        return 0
    cli_common._write_config(updated)
    print(f"[{lid}] {seat}: {old or '(none)'} → {login} ({kind}); resolved on GitHub as "
          f"{account.get('login')} (id {account.get('id')})")
    if old and old.lower() != login.lower() and seat in ("reviewer", "fixer"):
        print(f"  {old} stays on the allowlist (open PRs keep its verdicts and authorship) and its "
              "credential stays mapped; remove them once no open PR needs them")
    form_key = {**config.LOGIN_SETTINGS, "adjudicator": "adjudicator_login"}.get(seat)
    form_login = str(settings_form.settings_defaults(cli_common._SETTINGS).get(form_key) or "").strip() \
        if form_key else ""
    if form_login and form_login.lower() != login.lower():
        # #1296: `apply` refuses to move a seat off an App, but moves a PAT seat as the form says
        # (#1306): the note says which, so it never promises a refusal that will not come.
        then = ("or `apply` refuses to run" if cli_common._app_ref(updated, login) else
                f"or the next `apply` moves the {seat} back to {form_login}")
        print(f"  note: the plugin settings still name {form_login} for the {seat}: set "
              f"{form_key} to {login} (or blank it), {then}")
    steps = [f"hermes dk doctor --loop {lid}", f"hermes dk selftest --loop {lid} --no-model"]
    if seat == "reviewer" and config.merge_check_on(updated):
        steps.append(f"hermes dk protect --loop {lid} --admin-token LOGIN   (the reviewer is an "
                     "App: require diaktoros/review, and turn required approvals off on "
                     f"{updated.get('base')})")
    print("  next: " + "\n        ".join(steps))
    return 0


def add_identity(sub) -> None:  # noqa: ANN001
    """The ``identity`` subcommand's parser, registered by ``cli.register_cli`` (#1163)."""
    identity = sub.add_parser("identity", help="Move one identity of a loop (a seat, the reader or "
                                               "the adjudicator) to a GitHub App, or back to a PAT")
    identity.add_argument("--loop", required=True, help="loop id")
    identity.add_argument("--seat", required=True, choices=("reviewer", "fixer", "read", "adjudicator"),
                          help="which identity moves")
    identity.add_argument("--app", default="", metavar="LOGIN=APP_ID:KEY",
                          help="the App: <app-slug>[bot]=APP_ID:/abs/path/to/key.pem (mode 0600)")
    identity.add_argument("--pat", default="", metavar="LOGIN=PATH",
                          help="or a person's account: login=/abs/path/to/pat (mode 0600)")
    identity.add_argument("--dry-run", action="store_true",
                          help="prove the identity on GitHub and print the change; write nothing")
    identity.set_defaults(func=cmd_identity)


def add_fixer_push(sub) -> None:  # noqa: ANN001
    """The ``fixer-push`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    fixer_push = sub.add_parser("fixer-push", help="Explicit per-repository unattended fixer push policy")
    fixer_push.add_argument("--loop", required=True, help="exact loop id (never all loops)")
    direction = fixer_push.add_mutually_exclusive_group(required=True)
    direction.add_argument("--enable", action="store_true", help="opt this repository in")
    direction.add_argument("--disable", action="store_true", help="turn unattended pushes off")
    fixer_push.add_argument("--acknowledge-pr-race", action="store_true",
                            help="accept the residual non-atomic PR-metadata/ref race; required for --enable")
    fixer_push.add_argument("--dry-run", action="store_true", help="show action without writing")
    fixer_push.set_defaults(func=cmd_fixer_push)
