"""Installing a loop (#676): ``init`` writes it. ``setup``, which walks a first install through
``init``, ``arm`` and the checks, is ``cli/setup_command.py``'s and calls this module's
``_install_schedule`` as ``cli_install._install_schedule``.

``cli`` re-exports ``cmd_init``. The install plumbing they share with ``set``, ``apply`` and
``uninstall`` is ``cli_common``'s, called as ``cli_common.<name>``, and so are the settings
form's values and the root parser ``register_cli`` keeps.
"""
from __future__ import annotations

import pathlib
import shlex
import subprocess

from .. import (
    config,
    gate_shims,
    route_intent,
    routes,
)
from . import common as cli_common
from ..config import credentials
from ..config import lists
from ..config import settings_form
from .common import BRANCH_PUSH_REFUSAL, SHARED_JOB_NAMES, _hook_route_name


def _seat_lines(loop: dict, title: str = "seat mapping") -> list[str]:
    """The effective mapping: who serves each role, as what login, woken by which route.

    This is the line an operator reads to answer "is the right agent actually the fixer here?" without
    opening two JSON files — so it shows the resolved values, including the route URL the profile
    is part of.
    """
    lines = [f"{title}:"]
    names = cli_common._routes_of(loop)
    host = loop.get("host") or ""
    for role in config.ROUTE_ROLES:
        profile = config.seat_profile(loop, role) or "(none)"
        name = names.get(role) or ""
        if not name:
            lines.append(f"  {role:<12} profile {profile:<16} route (none — nothing wakes it)")
            continue
        url = ""
        if host:
            try:
                url = routes.url_for_profile(name, profile, host) or ""
            except config.ConfigError:
                url = ""
        login = config.seat_login(loop, role) or "(unset)"
        # The adjudicator has no login, so it keeps the column empty rather than shifting the route
        # and URL columns out of line in the one place an operator compares seats side by side.
        if role == "adjudicator":
            adj = config.adjudicator_login(loop)
            login_cell = f"login {adj:<16} " if adj else " " * 23
        else:
            login_cell = f"login {login:<16} "
        lines.append(f"  {role:<12} profile {profile:<16} {login_cell}"
                     f"route {name:<22} {url}".rstrip())
    return lines


def _observer_args(args, loop_id: str) -> dict:
    """The observer block a new loop starts with — empty when the feed was not asked for.

    Opt-in by construction: naming a profile (or a route) is the whole switch, and a loop without
    one behaves exactly as it did before this existed.
    """
    if not (args.observer_profile or args.observer_route):
        return {}
    observer_cfg = {"route": args.observer_route or f"{loop_id}-observe",
                    "profile": args.observer_profile or "default",
                    "deliver": args.observer_deliver}
    if args.observer_events:
        observer_cfg["events"] = args.observer_events
    if args.observer_digest_min:
        observer_cfg["digest_min"] = args.observer_digest_min
    if getattr(args, "observer_weekly_report", ""):
        # Checked again by normalize; refused here by name rather than written misconfigured.
        observer_cfg["weekly_report"] = config.check_weekly_report(args.observer_weekly_report)
    for key in ("urgent_route", "urgent_profile", "urgent_deliver"):
        if getattr(args, f"observer_{key}", ""):
            observer_cfg[key] = getattr(args, f"observer_{key}")
    return observer_cfg


def _hook_editor_line(loop: dict, token_login: str | None, armed: bool, dry_run: bool) -> str:
    """Who created (or would create) the hooks, in which state, and what `arm` will need."""
    who = token_login or loop.get("read_token")
    verb = "would be created" if dry_run else "were created"
    state = "armed (--arm)" if armed else "paused"
    return (f"hooks {verb} {state} as {who}, and `arm` / `arm --pause` edit them as {who} too: "
            f"{cli_common.hook_write_need(loop, token_login)}")


def _shared_job_present() -> bool:
    """Is the one shared watchdog job already scheduled? (idempotent init.)"""
    jobs, _ = cli_common._cron_jobs({"id": ""})
    if jobs is None:
        return False
    return any(str(job.get("name") or "").strip() in SHARED_JOB_NAMES for job in jobs)


def _remove_legacy_jobs(loop: dict) -> list[str]:
    """Remove this loop's pre-#60 per-loop watchdog job, if one exists.

    Called when ``init`` creates (or finds) the shared job: without it the install sits at
    two sweepers — the shared job and the stale per-loop one — until an operator removes the
    stale job by hand. The shared job is what sweeps every loop; the legacy job is this
    loop's own and is safe to remove here.
    """
    jobs, error = cli_common._cron_jobs(loop)
    if jobs is None:
        return [f"cron: could not check for a legacy job ({error})"]
    legacy = [job for job in jobs
              if str(job.get("name") or "").strip() == cli_common._legacy_job_name(loop)]
    if not legacy:
        return []
    hermes = cli_common._hermes_bin()
    if not hermes:
        return ["cron: no `hermes` on PATH to remove the legacy job"]
    lines = []
    for job in legacy:
        job_id = str(job.get("id") or "")
        name = str(job.get("name") or "")
        if not job_id:
            lines.append(f"cron: legacy job {name!r} has no id; remove it by hand")
            continue
        try:
            proc = subprocess.run([hermes, "cron", "remove", job_id],
                                  capture_output=True, text=True, timeout=120)
        except Exception as exc:
            lines.append(f"cron: removing legacy job {job_id} failed ({exc})")
            continue
        if proc.returncode != 0:
            lines.append(f"cron: removing legacy job {job_id} failed "
                         f"({(proc.stderr or proc.stdout).strip()[:200]})")
        else:
            lines.append(f"removed the pre-#60 per-loop job: {job_id} ({name})")
    return lines


def _install_schedule(loop: dict, schedule: str, deliver: str) -> tuple[list[str], bool]:
    """A cron shim plus the one shared job, through the scheduler's own CLI.

    The job is created **only if missing** (#60): one shared job sweeps every loop, so a second
    ``init`` for another loop must not add a duplicate that would sweep them all again. Returns
    ``(lines, ok)``; ``ok`` is false only when no job exists and one could not be created. The
    fallback command is shell-quoted — the job name has spaces — so it can be pasted as printed.
    """
    shim = cli_common._write_watchdog_shim()
    if _shared_job_present():
        lines = [f"watchdog already scheduled (shared job, deliver={deliver})", f"shim: {shim}"]
        lines.extend(_remove_legacy_jobs(loop))
        return lines, True
    hermes = cli_common._hermes_bin() or "hermes"
    cmd = [hermes, "cron", "create", schedule, "--name", config.watchdog_job_name(),
           "--no-agent", "--script", shim.name, "--deliver", deliver]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return [f"could not create the cron job: {exc}",
                f"run it yourself: {shlex.join(cmd)}"], False
    if proc.returncode != 0:
        return [f"cron create failed: {(proc.stderr or proc.stdout).strip()[:200]}",
                f"run it yourself: {shlex.join(cmd)}"], False
    lines = [f"scheduled the watchdog ({schedule}, deliver={deliver})", f"shim: {shim}"]
    # Only now — after the shared job is confirmed present or just created — remove this
    # loop's legacy job. Removing it earlier would leave no sweeper if creation failed.
    lines.extend(_remove_legacy_jobs(loop))
    return lines, True


def _stale_hooks_refusal(loop: dict, admin: str | None) -> str:
    """Why ``init --hooks`` must not create hooks yet, or ``""`` when the routes have none.

    Only hooks at this loop's route URLs collide; the same route name at another origin, profile
    or path is printed as information (the gateway never delivers it to these routes).

    Refuse, never adopt. Adopting would mean PATCHing a new secret onto hooks this install did not
    create: GitHub never returns a hook's secret, so nothing can prove whose they are or which of
    several is current, and an adopted hook keeps its old ``active`` state — an armed leftover
    would arm a loop that has not passed doctor/selftest. Refusing writes nothing, and the fix is
    one pasteable command per hook (or ``uninstall`` for a loop that is still configured).
    """
    hooks, foreign, error = cli_common._classify_hooks(loop, admin)
    if hooks is None:
        return (f"refused: cannot read {loop['repo']}'s hooks to check for a previous install's "
                f"({error}); nothing written. Give the --admin-token login hook access "
                "(`admin:repo_hook`, or classic `repo`) and re-run")
    for line in cli_common._foreign_lines(loop, foreign):
        print(f"  info: {line}")
    if not hooks:
        return ""
    ids = sorted(hook["id"] for hook in hooks)
    lines = [f"refused: {loop['repo']} already has repo hook(s) posting to this loop's routes "
             f"({', '.join(sorted({_hook_route_name(h) for h in hooks}))}): "
             f"{', '.join(str(i) for i in ids)} — "
             f"{sum(1 for h in hooks if h.get('active'))} active. They were created by a previous "
             "install and sign with its secret, which the routes this init writes will not hold.",
             "nothing written. Delete them (a token with `admin:repo_hook` or classic `repo`), "
             "then re-run this init:"]
    lines += [f"  {command}" for command in cli_common._hook_delete_commands(loop, ids)]
    return "\n".join(lines)


def _init_loop_concurrency(args, d: dict) -> int:
    return args.concurrency if args.concurrency is not None else settings_form.settings_loop_concurrency(d)


def _init_seat_concurrency(args, d: dict) -> dict:
    """The ``seats.<seat>.concurrency`` values ``init`` writes — only the ones that were asked for.

    A seat value wins over the loop default for good, so writing one nobody asked for pins that
    seat and makes ``--concurrency`` (and every later ``set --concurrency``) dead — issue #76.
    A per-seat flag is always written. Without one, a seat takes the settings form's own value
    only when the operator did not name a loop default *and* the form's seat differs from the one
    the form implies (reviewer 2, fixer 1): an explicit flag beats a form default, as everywhere.
    """
    loop_value = _init_loop_concurrency(args, d)
    out = {}
    for seat in ("reviewer", "fixer"):
        flag = getattr(args, f"{seat}_concurrency", None)
        if flag is not None:
            out[seat] = flag
        elif args.concurrency is None and d[f"{seat}_concurrency"] != loop_value:
            out[seat] = d[f"{seat}_concurrency"]
    return out


def _init_hooks_preview(loop: dict, args) -> list[str]:
    """``init --hooks --dry-run``'s hook lines (#1391): one hook per role ``_install_hooks``
    creates (``config.hook_roles``), not a fixed count, and none when events arrive through the
    GitHub App's webhook."""
    hooked = config.hook_roles(loop)
    if not hooked:
        return ["would create no repo hooks: events arrive through the GitHub App's webhook"]
    armed = getattr(args, "arm", False)
    kinds = ", ".join(f"{role}: {config.HOOK_EVENT[role]}" for role in hooked)
    state = "armed (--arm)" if armed else "paused until `arm`"
    return [f"would create {len(hooked)} repo hooks, one per role ({kinds}), {state}",
            _hook_editor_line(loop, args.admin_token, armed, True)]


def cmd_init(args) -> int:
    """Install a loop: write its config, its routes, and (on request) its hooks and cron job.

    The settings form supplies *who serves each seat* the same way it supplies the numeric knobs —
    as a default a new loop starts from — while explicit flags still win, because an operator
    installing a loop from the CLI on this machine should not have to depend on what a per-profile
    form happens to hold. Everything is validated (profiles, allowlists, credentials, route
    ownership) before the first file is written.
    """
    if getattr(args, "arm", False) and not args.hooks:
        print("--arm arms the repo hooks init creates; it needs --hooks")
        return 2
    loop_id = args.id or args.repo.split("/")[-1]
    # The rule the loader enforces: an id that does not name exactly one config file would be
    # written here and then break every loop-wide command that reads the directory.
    if (not loop_id or loop_id in (".", "..") or pathlib.Path(loop_id).name != loop_id
            or "\\" in loop_id or loop_id.startswith(".")):
        print(f"--id {loop_id!r} cannot name a loop config file: use letters, digits, '-', '_' "
              "or '.' (not leading, not a path)")
        return 2
    d = settings_form.settings_defaults(cli_common._SETTINGS)
    tokens = {}
    for pair in args.token or []:
        split = cli_common._split_token_pair(pair)
        if split is None:
            return 2
        login, path = split
        try:
            credentials.check_token_file(path, f"--token {login}")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
        tokens[login] = path
    apps = {}
    for pair in getattr(args, "app", None) or []:
        split = cli_common._split_app_pair(pair)
        if split is None:
            return 2
        apps[split[0]] = split[1]

    reviewer_profile = args.reviewer_profile or d["reviewer_profile"]
    fixer_profile = args.fixer_profile or d["fixer_profile"]
    fixers = [login for login in (args.fixer or []) if login] or (
        [d["fixer_login"]] if d["fixer_login"] else [])
    reviewers = [login for login in (args.reviewer or []) if login] or (
        [d["reviewer_login"]] if d["reviewer_login"] else [])
    if not fixers or not reviewers:
        print("--fixer and --reviewer name the GitHub logins this loop trusts (repeatable); with "
              "neither the flag nor the plugin settings naming them, there is no loop to install")
        return 2
    if not reviewer_profile or not fixer_profile:
        print("both seats need a Hermes profile: pass --reviewer-profile/--fixer-profile, or set "
              "them in the plugin settings (Capabilities → Plugins → diaktoros)")
        return 2
    configured_reviewer = d["reviewer_login"]
    reviewer_seat = args.reviewer_seat or (
        configured_reviewer if configured_reviewer in reviewers else "") or (
        reviewers[0] if len(reviewers) == 1 else "")
    if not reviewer_seat:
        print("with several reviewer logins, --reviewer-seat names which one this loop's route serves")
        return 2
    configured_fixer = d["fixer_login"]
    if configured_fixer and configured_fixer.lower() not in {login.lower() for login in fixers}:
        print(f"configured fixer login {configured_fixer!r} is not in the --fixer allowlist — "
              "name an eligible fixer in the plugin settings before installing this loop")
        return 2
    fixer_seat = next((login for login in fixers
                       if login.lower() == configured_fixer.lower()), fixers[0]) if configured_fixer else fixers[0]
    adjudicator_profile = args.adjudicator_profile or d["adjudicator_profile"] or "default"
    explicit_adj = getattr(args, "adjudicator_login", None)
    adjudicator_login = (explicit_adj if explicit_adj is not None
                         else (d["adjudicator_login"] if args.adjudicator_route else "")).strip()
    if adjudicator_login and not args.adjudicator_route:
        print("--adjudicator-login needs --adjudicator-route: the comment identity only ever posts "
              "a ruling, and without a breach route nothing rules")
        return 2
    # CLI credentials win; select and validate only effective form paths, as apply does.
    explicit = {login.lower() for login in (*tokens, *apps)}
    credential_form = {}
    for role, login in (("reviewer", reviewer_seat), ("fixer", fixer_seat),
                        ("adjudicator", adjudicator_login)):
        if role == "adjudicator" and d["adjudicator_login"] and (
                d["adjudicator_login"].lower() != login.lower()):
            continue  # A form credential for another named adjudicator is not this CLI identity.
        if login.lower() not in explicit:
            for suffix in ("token_file", "app_id", "app_key_file"):
                credential_form[f"{role}_{suffix}"] = d[f"{role}_{suffix}"]
    credential_loop = {"tokens": tokens, "apps": apps,
                       "seats": {"reviewer": {"login": reviewer_seat},
                                 "fixer": {"login": fixer_seat},
                                 "adjudicator": {"login": adjudicator_login}},
                       "adjudicator": {"route": args.adjudicator_route}}
    try:
        credentials.verify_token_settings(credential_form, loop=credential_loop)
        tokens, apps, _selected, _ignored = settings_form.select_credentials(
            credential_loop, credential_form)
    except config.ConfigError as exc:
        print(f"config refused: {exc}")
        return 2
    # The weekly report (#658): the flag wins over the form; the form's 'off' or blank is none.
    weekly_form = str(d["observer_weekly_report"]).strip()
    if (not getattr(args, "observer_weekly_report", "") and weekly_form.lower() not in ("", "off")
            and (args.observer_profile or args.observer_route)):     # only for a feed being made
        args.observer_weekly_report = weekly_form
    if getattr(args, "observer_weekly_report", ""):
        try:
            config.check_weekly_report(args.observer_weekly_report)
        except ValueError as exc:
            print(f"refused: --observer-weekly-report: {exc}")
            return 2

    # Shadow reviews (#218, #1004): the flags win, else the plugin settings. Built before the
    # loop dict so a bad value is a refusal, not a traceback.
    try:
        shadow = settings_form.shadow_block(
            {}, (args.shadow_profile if getattr(args, "shadow_profile", None) is not None
                 else settings_form._form_value(d, "shadow_profile")),
            (args.shadow_every if getattr(args, "shadow_every", None) is not None
             else settings_form._form_value(d, "shadow_every")), "init",
            escalate=(args.shadow_escalate if getattr(args, "shadow_escalate", None) is not None
                      else settings_form._form_value(d, "shadow_escalate")),
            file_issues=(args.shadow_file_issues
                         if getattr(args, "shadow_file_issues", None) is not None
                         else settings_form._form_value(d, "shadow_file_issues"))) or {}
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    raw = {
        "id": args.id or args.repo.split("/")[-1],
        "repo": args.repo, "base": args.base, "cap": args.cap,
        "concurrency": _init_loop_concurrency(args, d),
        "fixers": fixers, "reviewers": reviewers,
        "reviewer_seat": reviewer_seat,
        "seats": {
            "reviewer": {"profile": reviewer_profile, "route": "",
                         "login": reviewer_seat,
                         "agent": args.reviewer_agent},
            "fixer": {"profile": fixer_profile, "route": "",
                      "login": fixer_seat, "agent": args.fixer_agent},
            **({"adjudicator": {"login": adjudicator_login}} if adjudicator_login else {}),
        },
        "adjudicator": ({"route": args.adjudicator_route, "profile": adjudicator_profile}
                        if args.adjudicator_route else {}),
        # Shadow reviews (#218): the flags win, else the plugin settings (built above).
        "shadow": shadow,
        "skill": args.skill,
        "tokens": tokens, "read_token": args.read_token,
        **({"apps": apps} if apps else {}),
        "clone": args.clone, "roots": args.root or [],
        "state_dir": args.state_dir or str(config.default_state_dir(
            args.id or args.repo.split("/")[-1])),
        "host": args.host, "grace_min": args.grace_min,
        "ttl_min": args.ttl_min, "inflight_ttl_min": args.inflight_ttl_min,
        "turn_budget_s": getattr(args, "turn_budget", None),
        "observer": _observer_args(args, args.id or args.repo.split("/")[-1]),
        # Sign what the loop posts (#197): on unless --attribution off or the form says so.
        "attribution": cli_common._attribution_arg(args, d["attribution"]),
        # Each as asked, else the form's. review_only_update (host merge pushes to a review-only
        # author's branch) is also refused below unless acknowledged.
        **{key: cli_common._on_off(args, key, d[key]) for key in (
            "review_after_ci", "fix_ci", "fixer_scope_lock", "adjudicator_approves",
            "review_only_update")},
        # The checks CI always runs, which a fixer running only its touched tests would miss.
        "fixer_check": (d["fixer_check"] if getattr(args, "fixer_check", None) is None
                        else args.fixer_check),
        "merge_check": (getattr(args, "merge_check", None)
                        or str(d.get("merge_check") or "").strip().lower() or "auto"),
        "ingress": (getattr(args, "ingress", None)
                    or str(d.get("ingress") or "").strip().lower() or "repo-hooks"),
        "ingress_app": (getattr(args, "ingress_app", None) or str(d.get("ingress_app") or "").strip()),
        # The checks that gate an approval (#368); none named = every check gates.
        "required_checks": (lists.split_check_names(d["required_checks"])
                            if getattr(args, "required_check", None) is None
                            else [name for name in args.required_check if name.strip()]),
        # Paths only a human may approve (#478).
        "human_paths": (lists.split_human_paths(d["human_paths"])
                        if getattr(args, "human_path", None) is None
                        else [name for name in args.human_path if name.strip()]),
        # Generated files the host regenerates on a conflict (#551).
        "generated": lists.parse_generated(
            lists.split_generated(d["generated"]) if getattr(args, "generated", None) is None
            else args.generated, "--generated"),
        # What each CI check covers, told to the reviewer (#756).
        "ci_coverage": lists.parse_ci_coverage(
            lists.split_ci_coverage(d["ci_coverage"]) if getattr(args, "ci_covers", None) is None
            else args.ci_covers, "--ci-covers"),
        # Authors reviewed but never fixed (#191).
        "review_only": ([name.strip() for name in str(d["review_only"]).split(",") if name.strip()]
                        if getattr(args, "review_only", None) is None
                        else [name for name in args.review_only if name.strip()]),
        # Agents that post as the operator's account (#757).
        "operator_agents": ([name.strip() for name in str(d["operator_agents"]).split(",")
                             if name.strip()]
                            if getattr(args, "operator_agent", None) is None
                            else [name for name in args.operator_agent if name.strip()]),
    }
    if raw["review_only_update"] and not getattr(args, "acknowledge_branch_push", False):
        print(BRANCH_PUSH_REFUSAL)
        return 2
    # #539: CI-fix turns per PR; a flag wins over the form; empty = the default.
    try:
        raw["ci_fix_cap"] = lists.check_ci_fix_cap(
            getattr(args, "ci_fix_cap", None) if getattr(args, "ci_fix_cap", None) is not None
            else d["ci_fix_cap"], "init")
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    # The review-only verdict cap and daily cap: a flag wins over the form; empty = unset.
    for key in ("review_only_cap", "review_only_daily"):
        flag = getattr(args, key, None)
        try:
            raw[key] = lists._check_review_only_limit(
                flag if flag is not None else d[key], key, "init")
        except config.ConfigError as exc:
            print(f"refused: {exc}")
            return 2
    # A seat-level capacity wins over the loop default, so only write it when it was asked for.
    for seat, value in _init_seat_concurrency(args, d).items():
        raw["seats"][seat]["concurrency"] = value
    for seat, value in (("reviewer", getattr(args, "reviewer_turn_budget", None)),
                         ("fixer", getattr(args, "fixer_turn_budget", None))):
        if value is not None:
            raw["seats"][seat]["turn_budget_s"] = value
    # Turn knobs (#323): a flag wins over the form; 0 or blank is the role default (nothing written).
    try:
        for seat in ("reviewer", "fixer"):
            flag = getattr(args, f"{seat}_max_steps", None)
            steps = flag if flag is not None else config._form_int(d[f"{seat}_max_steps"].strip())
            if steps not in (0, ""):
                raw["seats"][seat]["max_steps"] = config._check_max_steps(
                    steps, f"seats.{seat}.max_steps", "init")
        for seat in ("reviewer", "fixer"):
            flag = getattr(args, f"{seat}_daily_tokens", None)
            budget = flag if flag is not None else config._form_int(
                d[f"{seat}_daily_tokens"].strip())
            if budget not in (0, ""):
                raw["seats"][seat]["daily_tokens"] = config._check_daily_tokens(
                    budget, f"seats.{seat}.daily_tokens", "init")
        # The shadow seat's token budget (#875): the flag, else the form.
        flag = getattr(args, "shadow_daily_tokens", None)
        budget = flag if flag is not None else config._form_int(
            str(d.get("shadow_daily_tokens") or "").strip())
        if budget not in (0, "", None):
            raw["seats"].setdefault("shadow", {})["daily_tokens"] = config._check_daily_tokens(
                budget, "seats.shadow.daily_tokens", "init")
        # The shadow and triage seats' own capacity: the flag, else the form; blank = unset.
        for seat in ("shadow", "triage"):
            flag = getattr(args, f"{seat}_concurrency", None)
            capacity = flag if flag is not None else config._form_int(
                str(d.get(f"{seat}_concurrency") or "").strip())
            if capacity not in ("", None):
                raw["seats"].setdefault(seat, {})["concurrency"] = config.check_concurrency(
                    capacity, f"seats.{seat}.concurrency", "init")
        flag = getattr(args, "fix_daily_turns", None)
        cap = flag if flag is not None else config._form_int(d["fix_daily_turns"].strip())
        if cap not in (0, ""):
            config._check_daily_turns(cap, "triage.fix_daily_turns", "init")
            # A new loop has no triage block (so no fix_label) for the cap to live in, and
            # `apply` refuses it for the same reason: refuse rather than accept and drop it.
            print(f"refused: the issue-fix daily cap {cap} cannot be set by init: a new loop "
                  "has no triage.fix_label for it to live in. Leave --fix-daily-turns (and the "
                  "fix_daily_turns setting) blank, then set it with `hermes dk triage "
                  "--fix-label LABEL --maintainer LOGIN --fix-daily-turns N`; nothing written")
            return 2
    except config.ConfigError as exc:
        print(f"refused: {exc}")
        return 2
    names = cli_common.routes_for(raw)
    raw["seats"]["reviewer"]["route"] = names["reviewer"]
    raw["seats"]["fixer"]["route"] = names["fixer"]
    roles = {"reviewer", "fixer"} | ({"adjudicator"} if raw["adjudicator"] else set())
    if raw["observer"].get("route"):
        roles.add("observer")
        if raw["observer"].get("urgent_route"):
            roles.add("observer_urgent")
    try:
        # The reader is named, never inferred: a default seat login (or the first token) is the
        # one-account-two-hats shape the broker refuses at the first write.
        if not args.read_token:
            raise config.ConfigError(
                "--read-token LOGIN names the account the gates read GitHub as (map its file with "
                f"--token LOGIN=/path/to/pat) — {config.FOUR_IDENTITY_RULE}")
        loop = config.normalize(raw)
        # One repo, one loop: a second id for it makes by_repo raise in every gate.
        others = config.loop_ids_for_repo(loop["repo"], exclude=loop["id"])
        if others:
            raise config.ConfigError(
                f"{loop['repo']} is already configured as loop {', '.join(repr(i) for i in others)}"
                " — change it with `hermes dk set`, or remove it first")
        # Routes are installed even without --hooks; never write a partial loop with
        # route URLs that cannot resolve to this operator's own gateway.
        config.webhook_host(loop["host"], required=True)
        # Who does what, and may they: profiles, allowlists, distinct credentials and route
        # ownership are all checked before a single file is written.
        # The adjudicator's token file is checked first, so a relative, missing or shared-readable
        # path is refused by its own name rather than as a generic missing file.
        credentials.verify_adjudicator_token(loop)
        credentials.verify_seats(loop, roles)
        # Hooks are created as --admin-token's login, after the config and routes are written;
        # an unmapped login would fail there and roll everything back, so refuse it up front.
        refusal = cli_common.admin_unmapped(loop, args.admin_token) if args.hooks else ""
        if refusal:
            raise config.ConfigError(refusal)
        cli_common._verify_routes(loop, roles)
        cli_common._observer_check(loop)
        # Refuses a foreign file (#105). Routes init is about to write are not "missing".
        shim_lines = gate_shims.install(loop, dry_run=True, report=False) + [
            f"⚠️ route {name}: {detail}" for name, (detail, _fix, _status)
            in gate_shims.divergence(loop, include_missing=False).items()]
    except config.ConfigError as exc:
        print(f"config refused: {exc}")
        return 2

    if args.dry_run:
        print("dry run — nothing written: no loop config, no routes, no hooks, no cron job")
        print(f"  would write: {config.config_dir() / (loop['id'] + '.json')}")
        for line in _seat_lines(loop, "effective seat mapping"):
            print(f"  {line}")
        for line in cli_common._parallel_lines(loop):
            print(f"  {line}")
        print("  credentials: " + " · ".join(cli_common._credential_lines(loop)))
        for name in cli_common._routes_of(loop).values():
            print(f"  would write route: {name}")
        if args.hooks:
            for line in _init_hooks_preview(loop, args):
                print(f"  {line}")
        if args.schedule:
            print(f"  would install the watchdog cron job ({args.schedule})")
        if loop.get("observer", {}).get("route"):
            print(f"  would write route: {loop['observer']['route']}")
        for line in shim_lines:
            print(f"  {line}")
        return 0

    path = config.config_dir() / f"{loop['id']}.json"
    if path.exists():
        print(f"refused: loop {loop['id']!r} already exists; use `hermes dk set` "
              "to change it without losing observer destination/receipt bindings")
        return 2
    observer_name = (loop.get("observer") or {}).get("route")
    if observer_name and routes.route(observer_name):
        print(f"refused: route {observer_name!r} already exists and is not this observer's route")
        return 2
    if args.hooks:
        # A previous install's hooks on these routes still sign with that install's secret, and
        # this init writes the routes with a fresh one: a second set would leave the old ones live
        # but unable to authenticate, next to new paused ones. Refused (not adopted) while nothing
        # is written yet; see _stale_hooks_refusal for why.
        refusal = _stale_hooks_refusal(loop, args.admin_token)
        if refusal:
            print(refusal)
            return 2
    previous_config = None
    previous_routes = {name: routes.route(name) for name in cli_common._routes_of(loop).values()}
    try:
        path = cli_common._write_config(loop)
    except Exception as exc:
        print(f"config install FAILED: {exc}; previous config unchanged")
        return 2
    print(f"loop config written: {path}")
    for line in _seat_lines(loop):
        print(line)
    for line in cli_common._parallel_lines(loop):
        print(f"  {line}")
    print("  credentials: " + " · ".join(cli_common._credential_lines(loop)))
    # A route that fails to land must not leave a loop installed with half its seats woken: the
    # config `init` just wrote and any route it just added are taken back out again, so a second
    # `init` starts from clean rather than from a loop nobody can see.
    try:
        written_routes = list(cli_common._install_routes(loop).values())
        # The plugin's private copy of what it just wrote (secret included) is part of the same
        # transaction: self-heal restores from it, so it must never describe routes that failed.
        route_intent.record_live(loop, written_routes, replace=True)
    except Exception as exc:
        try:
            routes.restore_entries(previous_routes)
            if previous_config is None:
                path.unlink(missing_ok=True)
            else:
                cli_common._restore_config(path, previous_config)
        except Exception as rollback_exc:
            print(f"ROLLBACK FAILED: {rollback_exc} — inspect config and routes manually")
            return 2
        print(f"route install FAILED: {exc}")
        print("  previous config and routes restored; fix the registry and re-run init")
        return 2
    for name in written_routes:
        print(f"route written: {name}")
    shims_ok = cli_common._install_shims(loop)
    try:
        hook_lines = (cli_common._install_hooks(loop, args.admin_token, active=bool(getattr(args, "arm", False)))
                      if args.hooks else [])
    except Exception as exc:
        failed = []
        try:
            routes.restore_entries(previous_routes)
        except Exception as rollback_exc:
            failed.append(f"routes: {rollback_exc}")
        try:
            route_intent.forget(loop, written_routes)
        except Exception as rollback_exc:
            failed.append(f"route intent: {rollback_exc}")
        try:
            if previous_config is None:
                path.unlink(missing_ok=True)
            else:
                cli_common._restore_config(path, previous_config)
        except Exception as rollback_exc:
            failed.append(f"config: {rollback_exc}")
        print(f"hook install FAILED: {exc}")
        if failed or "ROLLBACK FAILED" in str(exc):
            print("ROLLBACK FAILED — inspect before retrying: " + "; ".join(failed))
        else:
            print("  prior config and routes restored")
        return 2
    for line in hook_lines:
        print(f"  {line}")
    if args.hooks and not config.app_ingress(loop):
        print(f"  {_hook_editor_line(loop, args.admin_token, getattr(args, 'arm', False), False)}")
    if config.app_ingress(loop):
        # #1239: no repo hooks; the App's webhook is pointed at its route instead.
        print(f"  next: hermes dk app-webhook --loop {loop['id']} --set   (points "
              f"{loop.get('ingress_app')}'s webhook at route {config.app_route(loop)})")
    elif not args.hooks:
        print("  (repo hooks not created — pass --hooks, or add them by hand with the route URLs)")
    # Armed at birth: prove the secret now, as `arm` does, rather than at the first real event.
    pinged = (cli_common._ping_loop_hooks(loop, args.admin_token)
              if args.hooks and getattr(args, "arm", False) else True)
    schedule_lines, scheduled = (_install_schedule(loop, args.schedule, args.watchdog_deliver)
                                 if args.schedule else ([], True))
    for line in schedule_lines:
        print(f"  {line}")
    if not scheduled:
        # Config, routes and hooks are in place; only the job is missing. Say so and fail, so an
        # install script's `init && ...` does not read a missing watchdog as success.
        print("\ninit INCOMPLETE: the watchdog job was not scheduled — run the command above, "
              f"then `hermes dk doctor --loop {loop['id']}`")
        return 1
    if not pinged:
        print(f"\ninit INCOMPLETE: a hook's ping was rejected — see the ❌ line above, then "
              f"`hermes dk doctor --loop {loop['id']}`")
        return 1
    # The seats never hold a GitHub token: every write goes through the host broker with the token
    # files mapped above, so a GH_TOKEN in a seat profile's .env is only an extra copy to leak.
    lid = loop["id"]
    runtime = config.host_path("runtime")
    steps = ([f"create the runtime file {runtime} (`hermes dk setup --repo "
              f"{shlex.quote(loop['repo'])}` detects and writes it)"]
             if not runtime.exists() else []) + [
             f"hermes dk doctor --loop {lid}",
             f"hermes dk selftest --loop {lid} --no-model, then --pr N, then --pr N --live-turn"]
    if not config.unattended_fixer_push_enabled(loop):
        steps.append("decide the fix leg: unattended fixer pushes are off, so a changes-requested "
                     "verdict is held for you and no fixer turn starts. To let the fixer answer "
                     f"verdicts: {config.fixer_push_enable_command(loop)} "
                     "(read docs/operations.md on the PR-metadata race first)")
    if config.app_ingress(loop):
        pass                          # no repo hooks: the app-webhook step above is the next one
    elif args.hooks and not getattr(args, "arm", False):
        admin = f" --admin-token {args.admin_token}" if args.admin_token else ""
        steps.append(f"hermes dk arm --loop {lid}{admin}   (the hooks were created "
                     "paused)")
    elif args.hooks:
        steps.append("the hooks are ARMED: until the runtime file exists every turn is held")
    print("\nNext:")
    for n, step in enumerate(steps, 1):
        print(f"  {n}. {step}")
    print("  Seat tokens live only in the token files mapped above; a seat profile needs no "
          "GH_TOKEN.")
    return 0 if shims_ok else 1


# -- the parser for this module's commands (#676 step 7) --------------------------------------


def add_init(sub, d, both) -> None:  # noqa: ANN001
    """The ``init`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    init = sub.add_parser("init", help="Configure a loop and install its routes")
    init.add_argument("--repo", required=True, help="owner/name")
    init.add_argument("--id", help="loop id (default: the repository name)")
    init.add_argument("--fixer", action="append", default=[],
                      help="GitHub login that pushes (repeatable; default: the plugin setting)")
    init.add_argument("--reviewer", action="append", default=[],
                      help="GitHub login that may review (repeatable; default: the plugin setting)")
    init.add_argument("--reviewer-seat", help="the login the reviewer route serves")
    init.add_argument("--reviewer-profile", default=d["reviewer_profile"],
                      help="Hermes profile for the reviewer seat (default: the plugin setting)")
    init.add_argument("--fixer-profile", default=d["fixer_profile"],
                      help="Hermes profile for the fixer seat (default: the plugin setting)")
    init.add_argument("--reviewer-agent", default="", help="display name for the reviewer (default: profile)")
    init.add_argument("--fixer-agent", default="", help="display name for the fixer")
    init.add_argument("--cap", type=int, default=d["cap"],
                      help="verdicts allowed before adjudication")
    # None, not the settings value: a flag nobody passed must not be written as a seat
    # override, or the loop default is dead for that seat forever (#76). cmd_init fills in the
    # settings form's values at read time.
    init.add_argument("--concurrency", type=int, default=None,
                      help=f"default PRs per seat at once: 1 = serialized (default: {both}, "
                           "from the plugin settings). Above 1 needs --clone unless the "
                           "isolated worker's runtime file exists, because each "
                           "run then gets its own clone or sandbox. Override per seat with "
                           "--reviewer-concurrency / --fixer-concurrency.")
    init.add_argument("--reviewer-concurrency", type=int, default=None,
                      help="PRs the reviewer may work at once (overrides --concurrency; "
                           f"default: the loop's, settings {d['reviewer_concurrency']})")
    init.add_argument("--fixer-concurrency", type=int, default=None,
                      help="PRs the fixer may work at once (overrides --concurrency; "
                           f"default: the loop's, settings {d['fixer_concurrency']})")
    init.add_argument("--shadow-concurrency", type=int, default=None,
                      help="shadow reviews that may run at once, >= 1 (the shadow seat's own "
                           "value: --concurrency does not reach it) (default: the plugin "
                           "setting, else 1)")
    init.add_argument("--triage-concurrency", type=int, default=None,
                      help="issue triages that may run at once, >= 1 (the triage seat's own "
                           "value: --concurrency does not reach it) (default: the plugin "
                           "setting, else 1)")
    init.add_argument("--base", default=d["base"],
                      help="the branch PRs target; only PRs against it are reviewed")
    init.add_argument("--clone", default=d["clone"], help="local clone the runs may use")
    init.add_argument("--root", action="append", default=[], help="a directory reviews may clean (repeatable)")
    init.add_argument("--state-dir", default="",
                      help="where this loop keeps its state files (default: "
                           "~/.hermes/state/diaktoros/<id>; ~/.hermes/state/review-loops/<id> "
                           "is only a leftover until `migrate` moves it)")
    init.add_argument("--token", action="append", default=[], help="login=/path/to/pat (repeatable)")
    init.add_argument("--app", action="append", default=[], metavar="LOGIN=APP_ID:KEY",
                      help="a GitHub App identity instead of a PAT, for a seat, the reader, the adjudicator or the hook admin (--admin-token): <app-slug>[bot]=APP_ID:/abs/path/to/key.pem (repeatable; the key stays on the host, mode 0600)")
    init.add_argument("--read-token", default="",
                      help="required: login whose token reads GitHub — its own account, never "
                           "a seat or the adjudicator login (the four-identity rule)")
    init.add_argument("--skill", default="",
                      help="skill the seats are told to load. A plugin-provided skill is "
                           "qualified, e.g. diaktoros:review-loop")
    init.add_argument("--adjudicator-route", default="",
                      help="route name for the adjudicator (e.g. <id>-breach): setting it "
                           "turns adjudication on when the verdict cap is spent")
    init.add_argument("--adjudicator-login", default=None,
                      help="optional fourth GitHub account the ruling is also posted as (needs "
                           "--adjudicator-route and its own --token LOGIN=/path)")
    init.add_argument("--adjudicator-profile", default="",
                      help="Hermes profile for the adjudicator (default: the plugin setting, "
                           "else the launch profile)")
    init.add_argument("--observer-route", default="",
                      help="route name for the read-only observer feed "
                           "(default: <id>-observe)")
    init.add_argument("--observer-profile", default="",
                      help="Hermes profile the observer feed belongs to (its chat) — naming "
                           "one switches the feed on")
    init.add_argument("--observer-deliver", default="telegram",
                      help="where the gateway delivers the feed (telegram, discord, ...); "
                           "the feed never wakes an agent")
    init.add_argument("--observer-events", default="",
                      help="comma-separated transitions to send, from "
                           "opened,handoff,verdict,approved,escalation,ruling,stall,closed,"
                           "triaged,fixing,fixed,failed,held,conflict,ci_failed,updated,main_red,stale_approval,human_paths,ready_set "
                           "(default: all)")
    init.add_argument("--observer-digest-min", type=int, default=0,
                      help="batch the feed into one message per this many minutes "
                           "(0 = one notice per transition)")
    init.add_argument("--observer-weekly-report", default="",
                      help="send the week's report to the feed once a week, at a weekday "
                           "and UTC time like 'mon 09:00' (default: none)")
    init.add_argument("--observer-urgent-route", default="",
                      help="second route for urgent notices (failed, held, escalation, ruling, "
                           "stall, conflict, uncertain); routine ones keep the main feed")
    init.add_argument("--observer-urgent-profile", default="",
                      help="profile for the urgent route (default: the observer profile)")
    init.add_argument("--observer-urgent-deliver", default="",
                      help="where the gateway delivers urgent notices (default: the feed's)")
    init.add_argument("--host", default=d["host"],
                      help="your gateway webhook origin (required unless set in plugin settings)")
    init.add_argument("--grace-min", type=int, default=d["grace_min"],
                      help="minutes a PR may sit quiet before the watchdog reports a stall")
    init.add_argument("--ttl-min", type=int, default=d["ttl_min"],
                      help="how long a run may hold its seat slot")
    init.add_argument("--inflight-ttl-min", type=int, default=d["inflight_ttl_min"],
                      help="how long an in-flight mark blocks a second run at the same head")
    init.add_argument("--review-only-update", choices=("on", "off"), default=None,
                      help="push a clean merge of the base into a review-only author's PR "
                           "branch (same repository only; off by default; turning it on "
                           "needs --acknowledge-branch-push)")
    init.add_argument("--acknowledge-branch-push", action="store_true",
                      help="accept that the host pushes to a branch the loop does not own; "
                           "required to turn --review-only-update on")
    for flag, text in (
            ("--review-after-ci", "start each review after the head's checks finish (up to an "
                                  "hour)"),
            ("--fix-ci", "hand a failed required check on a fixer's PR to the fixer, one turn per "
                         "head; needs unattended fixer pushes"),
            ("--fixer-scope-lock", "hold a fix round that changes files its findings do not name "
                                   "for a maintainer; needs unattended fixer pushes"),
            ("--adjudicator-approves", "on an ACCEPT ruling with green checks, approve as the "
                                       "adjudicator login and dismiss the change request it "
                                       "overruled"),
            ("--attribution", "sign what the loop posts with 'Automated by Diaktoros'")):
        on = d[flag[2:].replace("-", "_")]
        init.add_argument(flag, choices=("on", "off"), default=None,
                          help=f"{text} (default {'on' if on else 'off'})")
    init.add_argument("--review-only-cap", default=None, metavar="N",
                      help="verdicts the reviewer gives one review-only PR before it waits "
                           "for `review --another-round`, 1-1000 (default: the plugin "
                           "setting, else the review cap)")
    init.add_argument("--ci-fix-cap", default=None, metavar="N",
                      help="CI-fix turns per PR before a red head waits for you, 1-10 "
                           "(default: the plugin setting, else 3)")
    init.add_argument("--review-only-daily", default=None, metavar="N",
                      help="reviewer turns a day on review-only PRs, 1-1000 (default: the "
                           "plugin setting, else no cap)")
    init.add_argument("--review-only", action="append", default=None,
                      help="a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it, or comma-separated) (default: the plugin setting)")
    init.add_argument("--operator-agent", action="append", default=None,
                      help="a GitHub login of an agent that posts as the operator's own account; its comments reach the seats as maintainer data, labelled operator agent (repeat it, or comma-separated) (default: the plugin setting)")
    init.add_argument("--required-check", action="append", default=None,
                      help="a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates) (default: the plugin setting)")
    init.add_argument("--shadow-profile", default=None,
                     help="the Hermes profile shadow reviews run as (#218): a second reviewer model whose verdicts are recorded, never posted (default: the plugin setting, else none)")
    init.add_argument("--shadow-every", type=int, default=None,
                     help="shadow one in this many reviewer turns, 1-1000 (default: the plugin setting, else 5)")
    init.add_argument("--shadow-escalate", choices=("on", "off"), default=None,
                      help="on: a PR is not ready to merge while its shadow review is running, or after it requested changes (#1004); needs --shadow-every 1 (default: the plugin setting, else off)")
    init.add_argument("--shadow-file-issues", choices=("on", "off"), default=None,
                      help="on: the issues a shadow review asks to file are filed by the reader once the real review exists and did not file them, labelled shadow-found, at most 5 a day (#914) (default: the plugin setting, else off)")
    init.add_argument("--human-path", action="append", default=None,
                      help="a glob pattern for paths only a human may approve: the loop's approval of a diff touching one is left to a person (repeat it; none = no path reserved) (default: the plugin setting)")
    init.add_argument("--generated", action="append", default=None, metavar="PATH[,PATH]=COMMAND",
                      help="generated files the host regenerates when a PR's only merge conflicts are in them: PATH[,PATH...]=COMMAND, run in the sandbox on the merged tree (repeat it; none = off) (default: the plugin setting)")
    init.add_argument("--ci-covers", action="append", default=None, metavar="CHECK=COVERS",
                      help="what a CI check covers, told to the reviewer (#756): CHECK=COVERS, e.g. 'tests (3.14)=full unit suite and harness' (repeat it; none = the reviewer is told nothing) (default: the plugin setting)")
    init.add_argument("--ingress", choices=("repo-hooks", "app-webhook"), default=None,
                      help="where events arrive: repo-hooks (the default: per-loop repo hooks) or app-webhook (a GitHub App's own webhook, --ingress-app; the loop then needs no repo hooks)")
    init.add_argument("--ingress-app", default=None, help="with --ingress app-webhook: the bot login of the GitHub App whose webhook delivers (mapped with --app)")
    init.add_argument("--merge-check", choices=("auto", "on", "off"), default=None,
                      help="post the diaktoros/review merge check: auto (on when the reviewer is a GitHub App), on (a PAT loop gets a commit status) or off")
    init.add_argument("--fixer-check", default=None,
                      help="one command the fixer runs before every push or issue-fix PR, besides its touched tests (chain several with &&; '' for none) (default: the plugin setting)")
    init.add_argument("--turn-budget", type=int, default=d["turn_budget_s"],
                      help="seconds one isolated seat turn may run, build and tests included "
                           f"(default {d['turn_budget_s']}; the sandbox is killed past it)")
    init.add_argument("--reviewer-turn-budget", type=int, default=None,
                      help="the reviewer seat's own turn budget in seconds (overrides --turn-budget)")
    init.add_argument("--fixer-turn-budget", type=int, default=None,
                      help="the fixer seat's own turn budget in seconds (overrides --turn-budget)")
    init.add_argument("--reviewer-max-steps", type=int, default=None,
                      help="agent steps one reviewer turn may take, 8-200 (0 = default 60) "
                           "(default: the plugin setting)")
    init.add_argument("--fixer-max-steps", type=int, default=None,
                      help="agent steps one fixer or issue-fix turn may take, 8-200 "
                           "(0 = default 80) (default: the plugin setting)")
    init.add_argument("--reviewer-daily-tokens", type=int, default=None,
                      help="most tokens the reviewer may spend per day; once spent the next "
                           "turn waits for midnight (0 = none) (default: the plugin setting)")
    init.add_argument("--shadow-daily-tokens", type=int, default=None,
                     help="most tokens shadow reviews may spend per day; once spent the next waits "
                          "for midnight (0 = none) (default: the plugin setting)")
    init.add_argument("--fixer-daily-tokens", type=int, default=None,
                      help="most tokens the fixer (and issue fixes) may spend per day "
                           "(0 = none) (default: the plugin setting)")
    init.add_argument("--fix-daily-turns", type=int, default=None,
                      help="issue-fix turns per day: refused here, a new loop has no fix "
                           "label; set it with `triage --fix-daily-turns N` (0 = ignore)")
    init.add_argument("--hooks", action="store_true", help="create the GitHub hooks too, paused until `arm`")
    init.add_argument("--arm", action="store_true",
                      help="with --hooks: create them armed (live at once) instead of paused")
    init.add_argument("--admin-token", default="", help="login whose token can create hooks")
    init.add_argument("--schedule", default="", help="e.g. 15m — install the watchdog cron job")
    init.add_argument("--watchdog-deliver", default="local", help="cron delivery target for watchdog alerts")
    init.add_argument("--dry-run", action="store_true",
                      help="print the seat mapping and what would be written, write nothing")
    init.set_defaults(func=cmd_init)
