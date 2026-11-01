"""``setup`` (#215): a first install in one command, split from ``cli/install.py`` (#1349).

It walks a new loop through the runtime file, ``init``, the shared watchdog job, ``doctor`` and
``selftest`` and the merge check, then ``arm`` when asked, running each step's own verb. The
schedule step is ``cli_install._install_schedule``, the same code as ``init --schedule``.
``cli`` re-exports ``cmd_setup``; patch this module's helpers here, where its callers read them.
"""
from __future__ import annotations

import pathlib
import shlex
import sys

from .. import config
from . import common as cli_common
from . import install as cli_install
from ..config import lists
from ..config import settings_form


def _verb(*argv: str) -> int:
    """Run another ``hermes dk`` verb in-process; argparse refusing it is exit 2."""
    try:
        parsed = cli_common._PARSER.parse_args(list(argv))
    except SystemExit:
        return 2
    return parsed.func(parsed)


def _ask(question: str, default: str, interactive: bool) -> str:
    """One answer: typed, or the default (always the default without a terminal)."""
    if not interactive:
        return default
    try:
        answer = input(f"   {question}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def _agree(question: str, default: bool, interactive: bool, unattended: bool) -> bool:
    """A yes/no; without a terminal (``--yes``) the answer is ``unattended``."""
    if not interactive:
        return unattended
    try:
        answer = input(f"   {question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    except EOFError:
        answer = ""
    return default if not answer else answer in ("y", "yes")


def _setup_runtime(args, interactive: bool) -> bool:
    """Step 1: keep the runtime file's working paths, detect the rest, write it 0600."""
    from .. import runtime_detect, seat_model
    file = runtime_detect.path()
    print(f"\n1. Runtime paths — {file}")
    current, why = runtime_detect.read()
    if why and why != "absent":
        print(f"   the current file cannot be used ({why}); it will be replaced")
    have = {key: current[key] for key in runtime_detect.HOST_KEYS
            if current and isinstance(current.get(key), str) and current[key]}
    broken = runtime_detect.problems(have) if have else {}
    detected = runtime_detect.detect()
    chosen, origin = {}, {}
    # The runtime is derived from the venv actually chosen, so it is resolved last — explicitly,
    # not by trusting HOST_KEYS to list "venv" before "runtime" (#242).
    order = [key for key in runtime_detect.HOST_KEYS if key != "runtime"] + ["runtime"]
    for key in order:
        if key == "runtime" and "venv" in chosen:
            # The runtime belongs to the venv actually chosen (given, kept or detected), never to
            # one detection found elsewhere.
            derived = runtime_detect.runtime_for(pathlib.Path(chosen["venv"]))
            detected = {**detected, "runtime": str(derived)} if derived else detected
        given = getattr(args, key, None)
        for source, value in (("given", given), ("kept", None if key in broken else have.get(key)),
                              ("detected", detected.get(key)), ("kept", have.get(key))):
            if value:
                chosen[key], origin[key] = str(pathlib.Path(value).expanduser()), source
                break
    left = runtime_detect.problems(chosen)
    for key in runtime_detect.HOST_KEYS:
        if key in left:
            print(f"   ❌ {key:<8} {left[key]} — pass --{key} PATH")
        else:
            print(f"   ✅ {key:<8} {chosen[key]} ({origin[key]})")
    if left:
        print("   not written: every path must check out first (the file is all or nothing)")
        return False
    settings = runtime_detect.merged(current, chosen)
    try:
        seat_model.parse_runtime(settings)
    except ValueError as exc:
        print(f"   ❌ the model overrides kept from the current file do not parse ({exc}); "
              f"fix {file} by hand")
        return False
    private = (current is not None and not file.is_symlink()
               and not file.stat().st_mode & 0o077)
    if current == settings and private:
        print("   in place — nothing to change")
        return True
    if args.dry_run:
        print(f"   would write {file} (0600)")
        return True
    if not _agree(f"Write {file.name}?", True, interactive, True):
        print("   not written")
        return False
    print(f"   written: {runtime_detect.write(settings)} (0600)")
    return True


def _setup_init_argv(args, repo: str, loop_id: str, interactive: bool) -> tuple[list[str], str]:
    """Step 2's answers as the ``init`` command line, and the hook admin login ("" for none)."""
    d = settings_form.settings_defaults(cli_common._SETTINGS)
    ask = lambda flag, question, default="": flag or _ask(question, default, interactive)  # noqa: E731
    reviewer = ask(args.reviewer, "reviewer GitHub login (the account that reviews)",
                   d["reviewer_login"])
    fixer = ask(args.fixer, "fixer GitHub login (the account that pushes fixes)", d["fixer_login"])
    reviewer_profile = ask(args.reviewer_profile, "reviewer's Hermes profile", d["reviewer_profile"])
    fixer_profile = ask(args.fixer_profile, "fixer's Hermes profile", d["fixer_profile"])
    # A login given a GitHub App identity (--app, #1163) has a key, not a PAT: no file is asked.
    app_logins = {str(pair).split("=", 1)[0].strip().lower()
                  for pair in (getattr(args, "app", None) or [])}
    has_app = lambda login: bool(login) and login.lower() in app_logins  # noqa: E731
    reviewer_file = "" if has_app(reviewer) else ask(
        args.reviewer_token, f"token file for {reviewer or 'the reviewer'}", d["reviewer_token_file"])
    fixer_file = "" if has_app(fixer) else ask(
        args.fixer_token, f"token file for {fixer or 'the fixer'}", d["fixer_token_file"])
    reader = ask(args.read_token, "reader login (its own account; the gates read GitHub as it)")
    reader_file = "" if has_app(reader) else ask(
        args.read_token_file, f"token file for {reader or 'the reader'}")
    host = ask(args.host, "your gateway's webhook origin (https://…)", d["host"])
    observer = (args.observer_profile if args.observer_profile is not None else
                _ask("Hermes profile whose chat gets the loop's notices (blank: none)", "",
                     interactive))
    after_ci = args.review_after_ci or (
        "on" if _agree("Start each review after the head's CI finishes (up to an hour)?",
                       d["review_after_ci"], interactive, d["review_after_ci"]) else "off")
    fix_ci = args.fix_ci or (
        "on" if _agree("Give a failed CI check on a fixer's PR to the fixer (needs fixer pushes)?",
                       d["fix_ci"], interactive, d["fix_ci"]) else "off")
    attribution = args.attribution or (
        "on" if _agree("Sign what the loop posts ('Automated by Diaktoros')?",
                       d["attribution"], interactive, d["attribution"]) else "off")
    adjudicator = (args.adjudicator_profile if args.adjudicator_profile is not None else
                   _ask("Adjudicate a PR whose rounds are spent? Profile [blank = no]",
                        d["adjudicator_profile"], interactive)).strip()
    check = (args.fixer_check if args.fixer_check is not None else
             _ask("a command the fixer always runs before publishing, the checks CI always runs "
                  "(blank: none)", d["fixer_check"], interactive))
    admin = (args.admin_token if args.admin_token is not None else
             _ask("hook admin login, to create the repo hooks (blank: add them yourself)", "",
                  interactive))
    admin_file = ""
    if admin and admin.lower() not in {x.lower() for x in (reviewer, fixer, reader) if x}:
        admin_file = ask(args.admin_token_file, f"token file for {admin}")
    argv = ["init", f"--repo={repo}", f"--id={loop_id}", f"--reviewer={reviewer}",
            f"--fixer={fixer}", f"--reviewer-profile={reviewer_profile}",
            f"--fixer-profile={fixer_profile}", f"--read-token={reader}", f"--host={host}",
            f"--attribution={attribution}", f"--review-after-ci={after_ci}", f"--fix-ci={fix_ci}"]
    for login, file in ((reviewer, reviewer_file), (fixer, fixer_file), (reader, reader_file),
                        (admin, admin_file)):
        if login and file:
            argv.append(f"--token={login}={pathlib.Path(file).expanduser()}")
    argv += [f"--app={pair}" for pair in (getattr(args, "app", None) or [])]
    if observer:
        argv.append(f"--observer-profile={observer}")
    if adjudicator:
        argv += [f"--adjudicator-route={loop_id}-breach", f"--adjudicator-profile={adjudicator}"]
    argv.append(f"--fixer-check={check}")
    if getattr(args, "merge_check", None):
        argv.append(f"--merge-check={args.merge_check}")
    if getattr(args, "ingress", None):
        argv.append(f"--ingress={args.ingress}")
    if getattr(args, "ingress_app", None):
        argv.append(f"--ingress-app={args.ingress_app}")
    required = (args.required_check if args.required_check is not None else
                lists.split_check_names(_ask(
                    "the CI checks that must pass before an approval, comma-separated, exactly as "
                    "GitHub names them (blank: every check)", d["required_checks"], interactive)))
    argv += [f"--required-check={name}" for name in required]
    humans = (args.human_path if getattr(args, "human_path", None) is not None else
              lists.split_human_paths(_ask(
                  "glob patterns for paths only a human may approve, comma-separated "
                  "(blank: none)", d["human_paths"], interactive)))
    argv += [f"--human-path={name}" for name in humans]
    generated = (args.generated if getattr(args, "generated", None) is not None else
                 lists.split_generated(d["generated"]))
    argv += [f"--generated={entry}" for entry in generated]
    covers = (args.ci_covers if getattr(args, "ci_covers", None) is not None else
              lists.split_ci_coverage(d["ci_coverage"]))
    argv += [f"--ci-covers={entry}" for entry in covers]
    shadow = (args.shadow_profile if getattr(args, "shadow_profile", None) is not None else
              _ask("a Hermes profile for shadow reviews: a second reviewer model whose verdicts "
                   "are recorded, never posted (blank: none)", d["shadow_profile"], interactive))
    if str(shadow or "").strip():
        argv.append(f"--shadow-profile={str(shadow).strip()}")
    if getattr(args, "shadow_every", None) is not None:
        argv.append(f"--shadow-every={args.shadow_every}")
    if getattr(args, "shadow_escalate", None) is not None:
        argv.append(f"--shadow-escalate={args.shadow_escalate}")
    if getattr(args, "shadow_file_issues", None) is not None:
        argv.append(f"--shadow-file-issues={args.shadow_file_issues}")
    reviewed = (args.review_only if getattr(args, "review_only", None) is not None else
                [name for name in _ask("GitHub logins whose PRs the reviewer reviews but the fixer "
                                       "never touches, comma-separated (blank: none)",
                                       d["review_only"], interactive).split(",") if name.strip()])
    argv += [f"--review-only={name.strip()}" for name in reviewed]
    agents = (args.operator_agent if getattr(args, "operator_agent", None) is not None else
              [name for name in str(d["operator_agents"]).split(",") if name.strip()])
    argv += [f"--operator-agent={name.strip()}" for name in agents]
    if getattr(args, "review_only_update", None) is not None:
        argv.append(f"--review-only-update={args.review_only_update}")
    if getattr(args, "fixer_scope_lock", None) is not None:
        argv.append(f"--fixer-scope-lock={args.fixer_scope_lock}")
    if getattr(args, "acknowledge_branch_push", False):
        argv.append("--acknowledge-branch-push")
    for flag in ("reviewer_max_steps", "fixer_max_steps", "reviewer_daily_tokens",
                 "fixer_daily_tokens", "shadow_daily_tokens", "fix_daily_turns",
                 "shadow_concurrency", "triage_concurrency",
                 "review_only_cap", "review_only_daily", "ci_fix_cap"):
        if getattr(args, flag, None) is not None:
            argv.append(f"--{flag.replace('_', '-')}={getattr(args, flag)}")
    if admin:
        argv += ["--hooks", f"--admin-token={admin}"]   # created paused; step 5 arms them
    return argv, admin


def cmd_setup(args) -> int:
    """A first install in one command, safe to re-run (#215).

    1. the runtime file: working paths kept, the rest detected (``runtime_detect``), written 0600;
    2. the loop: ``init``'s answers asked (the settings form's values as defaults), its dry run
       shown and confirmed, then ``init`` itself — skipped when the loop already exists;
    3. the shared watchdog job (created only if missing);
    4. ``doctor`` and ``selftest --no-model``: any ❌ stops here, with its fix line above;
    5. ``arm``, only on a clean pass and only when asked.

    Every step runs the same code as its own verb, so ``setup`` adds no second path to keep right.
    """
    interactive = not args.yes
    if interactive and not sys.stdin.isatty():
        print("setup asks questions; with no terminal, pass --yes to take the flags and the plugin "
              "settings as the answers")
        return 2
    repo = args.repo or _ask("GitHub repository (owner/name)", "", interactive)
    if not repo or repo.count("/") != 1 or repo.startswith("/") or repo.endswith("/"):
        print("setup needs the repository as owner/name (--repo)")
        return 2
    loop_id = args.id or repo.split("/")[-1]
    print(f"hermes dk setup — {repo} (loop {loop_id!r}). Safe to re-run: what is already "
          "in place is kept." + (" Dry run: nothing is written." if args.dry_run else ""))

    runtime_ok = _setup_runtime(args, interactive)

    print("\n2. The loop")
    existing = (config.config_dir() / f"{loop_id}.json").exists()
    admin = args.admin_token or ""
    if existing:
        try:
            loop = config.load_id(loop_id)
        except config.ConfigError as exc:
            print(f"   ❌ loop {loop_id!r} exists but does not load: {exc}")
            return 2
        if loop["repo"].lower() != repo.lower():
            print(f"   ❌ loop {loop_id!r} serves {loop['repo']}, not {repo} — pass --id to name "
                  "a new loop")
            return 2
        print(f"   ✅ loop {loop_id!r} is configured — kept as it is (change it with `hermes "
              f"dk set --loop {loop_id}`)")
    else:
        argv, admin = _setup_init_argv(args, repo, loop_id, interactive)
        print("   init dry run:")
        if _verb(*argv, "--dry-run") != 0:
            print("\nsetup stopped: init refused the answers — fix what it names and re-run setup")
            return 2
        if args.dry_run:
            print("\n3. would schedule the shared watchdog job (if missing)\n"
                  "4. would run doctor and selftest --no-model\n5. would offer to require the "
                  "diaktoros/review merge check, when the loop posts it\n6. would arm only when "
                  "asked")
            return 0
        if not _agree("Install this loop?", True, interactive, True):
            print("setup stopped: nothing installed")
            return 1
        if _verb(*argv) == 2:
            print("\nsetup stopped: init failed — see above; re-running setup is safe")
            return 2

    print("\n3. The watchdog")
    schedule = args.schedule or _ask("how often the watchdog sweeps", "15m", interactive)
    deliver = args.watchdog_deliver or _ask("where its alerts go (local, telegram, …)", "local",
                                            interactive)
    if args.dry_run:
        print(f"   would schedule the shared watchdog job ({schedule}, deliver={deliver}) "
              "if it is missing")
        return 0
    lines, scheduled = cli_install._install_schedule(config.load_id(loop_id), schedule, deliver)
    for line in lines:
        print(f"   {'✅' if scheduled else '❌'} {line}")

    print("\n4. Checks")
    doctor_rc = _verb("doctor", f"--loop={loop_id}")
    selftest_rc = _verb("selftest", f"--loop={loop_id}", "--no-model")
    if doctor_rc or selftest_rc or not scheduled or not runtime_ok:
        print("\nsetup stopped before arming: fix each ❌ above (its fix line says how), then "
              f"re-run `hermes dk setup --repo {shlex.quote(repo)}` — it keeps what is done")
        return 1

    _setup_merge_check(args, config.load_id(loop_id), admin, interactive)

    print("\n6. Arm")
    if not (args.arm or _agree("Arm the repo hooks now? The loop goes live.", False,
                               interactive, False)):
        print(f"   not armed. When ready: hermes dk arm --loop {loop_id}"
              + (f" --admin-token {admin}" if admin else ""))
        return 0
    if not admin:
        admin = _ask("hook admin login", "", interactive)
    rc = _verb("arm", f"--loop={loop_id}", *([f"--admin-token={admin}"] if admin else []))
    print("\nsetup complete: the loop is live" if rc == 0 else
          "\narm was not confirmed — see above; re-running setup is safe")
    return rc


def _setup_merge_check(args, loop: dict, admin: str, interactive: bool) -> None:
    """Step 5 (#1163 M2c): when the loop posts ``diaktoros/review`` and the base does not require
    it yet, offer the loop's own ruleset. Never blocks setup: the check is still posted, and
    doctor keeps saying the base does not require it."""
    from .. import protection
    if not config.merge_check_on(loop):
        return
    print("\n5. The merge check")
    found = protection.read(loop, admin or None)
    lid = loop["id"]
    command = f"hermes dk protect --loop {lid}" + (f" --admin-token {admin}" if admin else "")
    if found.known and found.required:
        print(f"   ✅ {loop['base']} already requires {protection.RULESET_NAME} (doctor above "
              "says whether it is pinned)")
        return
    if not (args.protect or _agree(
            f"Require {protection.RULESET_NAME} on {loop['base']}? No one, admins included, "
            "merges a PR until the loop's check is green.", False, interactive, False)):
        print(f"   not required: the check is posted, but the merge button ignores it. "
              f"When ready: {command}")
        return
    done, line = protection.require(loop, admin or None)
    print(f"   {'✅' if done else '❌'} {line}")
    if not done:
        print(f"   re-run it when the login has admin access: {command}")
    elif protection.reviewer_app_id(loop) and found.approvals:
        print(f"   ⚠️  {loop['base']} still requires {found.approvals} approving review(s); "
              "GitHub never counts the reviewer App's, so turn them off in repo settings → "
              "rules, or approve every PR yourself")


# -- the parser for this module's command (#676 step 7) ---------------------------------------


def add_setup(sub) -> None:  # noqa: ANN001
    """The ``setup`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    first = sub.add_parser("setup", help="First install in one command: runtime paths, init, "
                                         "watchdog, doctor, selftest, then arm when asked "
                                         "(safe to re-run)")
    first.add_argument("--repo", help="owner/name (asked when not given)")
    first.add_argument("--id", help="loop id (default: the repository name)")
    first.add_argument("--yes", action="store_true",
                       help="no questions: the flags and the plugin settings are the answers, "
                            "and every confirmation is yes (arming still needs --arm)")
    first.add_argument("--dry-run", action="store_true", help="show every step, write nothing")
    first.add_argument("--protect", action="store_true",
                       help="require the diaktoros/review merge check on the base (the loop's "
                            "own ruleset; `hermes dk protect`) when the loop posts it")
    first.add_argument("--arm", action="store_true",
                       help="arm the hooks after a clean doctor and selftest")
    for flag, what in (("reviewer", "reviewer GitHub login"), ("fixer", "fixer GitHub login"),
                       ("reviewer-profile", "reviewer's Hermes profile"),
                       ("fixer-profile", "fixer's Hermes profile"),
                       ("reviewer-token", "reviewer's token file"),
                       ("fixer-token", "fixer's token file"),
                       ("read-token", "reader login (its own account)"),
                       ("read-token-file", "reader's token file"),
                       ("host", "your gateway's webhook origin"),
                       ("admin-token-file", "hook admin's token file"),
                       ("schedule", "watchdog interval (default 15m)"),
                       ("watchdog-deliver", "where watchdog alerts go (default local)")):
        first.add_argument(f"--{flag}", default="", help=what)
    first.add_argument("--app", action="append", default=None, metavar="LOGIN=APP_ID:KEY",
                       help="a GitHub App identity instead of a PAT, for a seat, the reader, "
                            "the adjudicator or the hook admin: "
                            "<app-slug>[bot]=APP_ID:/abs/path/to/key.pem (repeatable); that "
                            "login's token file is then not asked for")
    first.add_argument("--admin-token", default=None,
                       help="hook admin login: the hooks are created (paused) as it")
    first.add_argument("--observer-profile", default=None,
                       help="Hermes profile whose chat gets the loop's notices")
    first.add_argument("--adjudicator-profile", default=None,
                       help="Hermes profile that rules when a PR's verdict cap is spent; "
                            "turns adjudication on (blank: off) (default: the plugin setting)")
    first.add_argument("--review-only-cap", default=None, metavar="N",
                       help="verdicts the reviewer gives one review-only PR before it waits "
                            "for `review --another-round`, 1-1000 (default: the plugin "
                            "setting, else the review cap)")
    first.add_argument("--ci-fix-cap", default=None, metavar="N",
                       help="CI-fix turns per PR before a red head waits for you, 1-10 "
                            "(default: the plugin setting, else 3)")
    first.add_argument("--review-only-daily", default=None, metavar="N",
                       help="reviewer turns a day on review-only PRs, 1-1000 (default: the "
                            "plugin setting, else no cap)")
    first.add_argument("--review-only", action="append", default=None,
                       help="a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it, or comma-separated) (default: the plugin setting)")
    first.add_argument("--operator-agent", action="append", default=None,
                       help="a GitHub login of an agent that posts as the operator's own account; its comments reach the seats as maintainer data, labelled operator agent (repeat it, or comma-separated) (default: the plugin setting)")
    first.add_argument("--required-check", action="append", default=None,
                       help="a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates) (default: the plugin setting)")
    first.add_argument("--shadow-profile", default=None,
                      help="the Hermes profile shadow reviews run as (#218): a second reviewer model whose verdicts are recorded, never posted (default: the plugin setting, else none)")
    first.add_argument("--shadow-every", type=int, default=None,
                      help="shadow one in this many reviewer turns, 1-1000 (default: the plugin setting, else 5)")
    first.add_argument("--shadow-escalate", choices=("on", "off"), default=None,
                       help="on: a PR is not ready to merge while its shadow review is running, or after it requested changes (#1004); needs --shadow-every 1 (default: the plugin setting, else off)")
    first.add_argument("--shadow-file-issues", choices=("on", "off"), default=None,
                       help="on: the issues a shadow review asks to file are filed by the reader once the real review exists and did not file them, labelled shadow-found, at most 5 a day (#914) (default: the plugin setting, else off)")
    first.add_argument("--human-path", action="append", default=None,
                       help="a glob pattern for paths only a human may approve: the loop's approval of a diff touching one is left to a person (repeat it; none = no path reserved) (default: the plugin setting)")
    first.add_argument("--generated", action="append", default=None, metavar="PATH[,PATH]=COMMAND",
                       help="generated files the host regenerates when a PR's only merge conflicts are in them: PATH[,PATH...]=COMMAND, run in the sandbox on the merged tree (repeat it; none = off) (default: the plugin setting)")
    first.add_argument("--ci-covers", action="append", default=None, metavar="CHECK=COVERS",
                       help="what a CI check covers, told to the reviewer (#756): CHECK=COVERS, e.g. 'tests (3.14)=full unit suite and harness' (repeat it; none = the reviewer is told nothing) (default: the plugin setting)")
    first.add_argument("--ingress", choices=("repo-hooks", "app-webhook"), default=None,
                       help="where events arrive: repo-hooks (the default: per-loop repo hooks) or app-webhook (a GitHub App's own webhook, --ingress-app; the loop then needs no repo hooks)")
    first.add_argument("--ingress-app", default=None, help="with --ingress app-webhook: the bot login of the GitHub App whose webhook delivers (mapped with --app)")
    first.add_argument("--merge-check", choices=("auto", "on", "off"), default=None,
                       help="post the diaktoros/review merge check: auto (on when the reviewer is a GitHub App), on (a PAT loop gets a commit status) or off")
    first.add_argument("--fixer-check", default=None,
                       help="one command the fixer runs before every push or issue-fix PR, besides its touched tests (chain several with &&; '' for none) (default: the plugin setting)")
    first.add_argument("--review-only-update", choices=("on", "off"), default=None,
                       help="push a clean merge of the base into a review-only author's PR "
                            "branch (same repository only; off by default; turning it on "
                            "needs --acknowledge-branch-push)")
    first.add_argument("--acknowledge-branch-push", action="store_true",
                       help="accept that the host pushes to a branch the loop does not own; "
                            "required to turn --review-only-update on")
    first.add_argument("--review-after-ci", choices=("on", "off"), default=None,
                       help="start each review after the head's checks finish (up to an hour) (default: the plugin setting, off)")
    first.add_argument("--fix-ci", choices=("on", "off"), default=None,
                       help="hand a failed required check on a fixer's PR to the fixer "
                            "(default: the plugin setting, off)")
    first.add_argument("--fixer-scope-lock", choices=("on", "off"), default=None,
                       help="hold a fix round that changes files its findings do not name for "
                            "a maintainer (default: the plugin setting, off)")
    first.add_argument("--attribution", choices=("on", "off"), default=None,
                       help="sign what the loop posts with 'Automated by Diaktoros' "
                            "(default: the plugin setting, on)")
    first.add_argument("--reviewer-max-steps", type=int, default=None,
                       help="agent steps one reviewer turn may take, 8-200 (0 = default 60) "
                            "(default: the plugin setting)")
    first.add_argument("--fixer-max-steps", type=int, default=None,
                       help="agent steps one fixer or issue-fix turn may take, 8-200 "
                            "(0 = default 80) (default: the plugin setting)")
    first.add_argument("--reviewer-daily-tokens", type=int, default=None,
                       help="most tokens the reviewer may spend per day; once spent the next "
                            "turn waits for midnight (0 = none) (default: the plugin setting)")
    first.add_argument("--shadow-daily-tokens", type=int, default=None,
                      help="most tokens shadow reviews may spend per day; once spent the next waits "
                           "for midnight (0 = none) (default: the plugin setting)")
    first.add_argument("--shadow-concurrency", type=int, default=None,
                       help="shadow reviews that may run at once, >= 1 (default: the plugin "
                            "setting, else 1)")
    first.add_argument("--triage-concurrency", type=int, default=None,
                       help="issue triages that may run at once, >= 1 (default: the plugin "
                            "setting, else 1)")
    first.add_argument("--fixer-daily-tokens", type=int, default=None,
                       help="most tokens the fixer (and issue fixes) may spend per day "
                            "(0 = none) (default: the plugin setting)")
    first.add_argument("--fix-daily-turns", type=int, default=None,
                       help="issue-fix turns per day: refused here, a new loop has no fix "
                            "label; set it with `triage --fix-daily-turns N` (0 = ignore)")
    for key in ("source", "venv", "runtime", "rust"):
        first.add_argument(f"--{key}", default="",
                           help=f"runtime file's {key} path (default: detected)")
    first.set_defaults(func=cmd_setup)
