"""``setup`` (#215): a first install in one command, split from ``cli/install.py`` (#1349).

It walks a new loop through the runtime file, ``init``, the shared watchdog job, ``doctor`` and
``selftest`` and the merge check, then ``arm`` when asked, running each step's own verb. The
schedule step is ``cli_install._install_schedule``, the same code as ``init --schedule``.
``cli`` re-exports ``cmd_setup``; patch this module's helpers here, where its callers read them.
Its argument parser is ``setup_parser``.
"""
from __future__ import annotations

import pathlib
import shlex
import sys

from .. import config, onboarding, util
from . import checks as cli_checks
from . import common as cli_common
from . import install as cli_install
from . import seat_flags
from ..config import lists
from ..config import prices
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


def _chosen_node(args, current: dict | None, detected: dict) -> tuple[str | None, str]:
    """The runtime file's optional ``node`` (#1149) and where it came from. A re-run refreshes it:
    ``--node`` (``""`` is none), else the Node detected now, else the current file's while it
    still works (one detection can't find), else ``(None, "")``. A ``/usr`` install the sandbox
    already sees is detected and recorded too, though nothing is mounted for it: it is the Node
    seats get, so it replaces an older one (#2478 review 6, F11). A given path is recorded as its
    real directory."""
    from .. import runtime_detect
    given = getattr(args, "node", None)
    if given is not None:
        if not given:
            return "", "given"
        real = runtime_detect.node_canonical(given)
        return str(real or pathlib.Path(given).expanduser()), "given"
    found = detected.get(runtime_detect.NODE_KEY)
    if found:
        return found, "detected"
    kept = (current or {}).get(runtime_detect.NODE_KEY)
    if isinstance(kept, str) and kept and not runtime_detect.node_problem(kept):
        return kept, "kept"
    return None, ""


def _setup_runtime(args, interactive: bool) -> bool:
    """Step 1: keep the runtime file's working paths, detect the rest, write it 0600."""
    from .. import runtime_detect, seat_model
    if sys.platform == "darwin" and (why := runtime_detect.native_unavailable()):
        print(f"   ❌ native prerequisites: {why}")
        return False
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
    node, origin["node"] = _chosen_node(args, current, detected)
    if node is not None:
        chosen[runtime_detect.NODE_KEY] = node
    left = runtime_detect.problems(chosen)
    if runtime_detect.node_problem(node):
        left["node"] = runtime_detect.node_problem(node)
    for key in runtime_detect.HOST_KEYS:
        if key in left:
            print(f"   ❌ {key:<8} {left[key]} — pass --{key} PATH")
        else:
            print(f"   ✅ {key:<8} {chosen[key]} ({origin[key]})")
    if "node" in left:
        print(f"   ❌ node     {left['node']} — pass --node PATH, or --node \"\" for none")
    elif node:
        print(f"   ✅ node     {node} ({origin['node']})")
    else:
        how = f" ({origin['node']})" if origin["node"] else ""
        print(f"   ➖ node     none{how}: no extra Node runtime")
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


def _given(args, flags: tuple) -> list[str]:
    """``--flag=value`` for each of ``flags`` the command line set, passed on to ``init`` as
    given; one left unset (None) is not passed, so ``init`` applies its own default."""
    return [f"--{flag.replace('_', '-')}={getattr(args, flag)}" for flag in flags
            if getattr(args, flag, None) is not None]


def _switch(flag, question: str, default: bool, interactive: bool) -> str:
    """An on/off answer for ``init``: the flag when given, else asked (the form's ``default`` when
    there is no one to ask)."""
    return flag or ("on" if _agree(question, default, interactive, default) else "off")


def _app_logins_test(args):
    """A predicate: whether a login was given a GitHub App identity with ``--app`` (#1163).

    Such a login has a key, not a PAT, so no token file is asked for it. Logins compare by
    ``util.login_key``, the rule the credential maps use (#1210)."""
    app_logins = {util.login_key(str(pair).split("=", 1)[0].strip())
                  for pair in (getattr(args, "app", None) or [])}
    return lambda login: bool(login) and util.login_key(login) in app_logins


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
    has_app = _app_logins_test(args)
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
    after_ci = _switch(args.review_after_ci, "Start each review after the head's CI finishes "
                       "(up to an hour)?", d["review_after_ci"], interactive)
    fix_ci = _switch(args.fix_ci, "Give a failed CI check on a fixer's PR to the fixer (needs "
                     "fixer pushes)?", d["fix_ci"], interactive)
    attribution = _switch(args.attribution, "Sign what the loop posts ('Automated by Diaktoros')?",
                          d["attribution"], interactive)
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
    if (admin and not has_app(admin)
            and util.login_key(admin) not in {util.login_key(x) for x in (reviewer, fixer, reader) if x}):
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
    argv += _setup_setting_argv(args, d, interactive)
    if admin:
        argv += ["--hooks", f"--admin-token={admin}"]   # created paused; step 5 arms them
    return argv, admin


def _setup_setting_argv(args, d: dict, interactive: bool) -> list[str]:
    """The settings part of step 2's ``init`` command line: each from its flag, else asked
    (or the form's value), in ``init``'s own flag spelling."""
    argv: list[str] = []
    if getattr(args, "merge_check", None):
        argv.append(f"--merge-check={args.merge_check}")
    if getattr(args, "ingress", None):
        argv.append(f"--ingress={args.ingress}")
    if getattr(args, "ingress_app", None):
        argv.append(f"--ingress-app={args.ingress_app}")
    argv += _setup_check_argv(args, d, interactive)
    shadow = (args.shadow_profile if getattr(args, "shadow_profile", None) is not None else
              _ask("a Hermes profile for shadow reviews: a second reviewer model whose verdicts "
                   "are recorded, never posted (blank: none)", d["shadow_profile"], interactive))
    if str(shadow or "").strip():
        argv.append(f"--shadow-profile={str(shadow).strip()}")
    argv += _given(args, ("shadow_every", "shadow_escalate", "shadow_file_issues",
                          "shadow_issues_daily"))
    for flag in ("shadow_always_path", "shadow_never_path"):     # #1788: else init's plugin setting
        argv += [f"--{flag.replace('_', '-')}={glob}" for glob in getattr(args, flag, None) or []]
    reviewed = (args.review_only if getattr(args, "review_only", None) is not None else
                [name for name in _ask("GitHub logins whose PRs the reviewer reviews but the fixer "
                                       "never touches, comma-separated (blank: none)",
                                       d["review_only"], interactive).split(",") if name.strip()])
    argv += [f"--review-only={name.strip()}" for name in reviewed]
    agents = (args.operator_agent if getattr(args, "operator_agent", None) is not None else
              [name for name in str(d["operator_agents"]).split(",") if name.strip()])
    argv += [f"--operator-agent={name.strip()}" for name in agents]
    argv += _given(args, ("review_only_update", "ci_webhook", "comment_webhook", "protection_webhook",
                          "fixer_scope_lock",
                          "adjudicator_approves",
                          "adjudicator_file_issues", "change_proof", "main_flake_rerun",
                          "update_behind", "review_only_rerequest_grants"))
    if getattr(args, "acknowledge_branch_push", False):
        argv.append("--acknowledge-branch-push")
    argv += _given(args, seat_flags.forwarded())
    argv += _given(args, ("fix_daily_turns", "review_only_cap", "review_only_daily", "ci_fix_cap",
                          "change_proof_max", "provider_breaker_failures", "reply_settle_seconds"))
    return argv

def _setup_check_argv(args, d: dict, interactive: bool) -> list[str]:
    """Step 2's checks and paths for ``init``: required checks, reserved and generated paths,
    and what each check covers."""
    argv: list[str] = []
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
    priced = (args.price if getattr(args, "price", None) is not None else
              prices.split_prices(d["prices"]))
    argv += [f"--price={entry}" for entry in priced]
    return argv


def cmd_setup(args) -> int:
    """A first install in one command, safe to re-run (#215).

    1. the runtime file: working paths kept, the rest detected (``runtime_detect``), written 0600;
    2. the loop: ``init``'s answers asked (the settings form's values as defaults), its dry run
       shown and confirmed, then ``init`` itself — skipped when the loop already exists;
    3. the shared watchdog job (created only if missing);
    4. ``doctor`` and ``selftest --no-model``: any ❌ stops here, with its fix line above;
    5. the merge check: when the loop posts ``diaktoros/review``, an offer to require it on the
       base (``protect``); otherwise one line saying there is nothing to require;
    6. ``arm``, only on a clean pass and only when asked.

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
    if sys.platform == "darwin" and not runtime_ok:
        return 1

    rc, admin = _setup_loop(args, repo, loop_id, interactive)
    if rc is not None:
        return rc
    rc, scheduled = _setup_watchdog(args, loop_id, interactive)
    if rc is not None:
        return rc
    if (rc := _setup_checks(repo, loop_id, scheduled, runtime_ok)) is not None:
        return rc
    _setup_merge_check(args, config.load_id(loop_id), admin, interactive)
    return _setup_arm(args, loop_id, admin, interactive)


def _setup_loop(args, repo: str, loop_id: str,
                interactive: bool) -> tuple[int | None, str]:
    """Step 2: the loop, kept when it exists, else ``init``'s answers, its dry run and ``init``.
    ``(rc, admin)``: rc is where setup stops (None: go on); admin, the hook admin login."""
    print("\n2. The loop")
    existing = (config.config_dir() / f"{loop_id}.json").exists()
    admin = args.admin_token or ""
    if existing:
        try:
            loop = config.load_id(loop_id)
        except config.ConfigError as exc:
            print(f"   ❌ loop {loop_id!r} exists but does not load: {exc}")
            return 2, admin
        if loop["repo"].lower() != repo.lower():
            print(f"   ❌ loop {loop_id!r} serves {loop['repo']}, not {repo} — pass --id to name "
                  "a new loop")
            return 2, admin
        print(f"   ✅ loop {loop_id!r} is configured — kept as it is (change it with `hermes "
              f"dk set --loop {loop_id}`)")
    else:
        argv, admin = _setup_init_argv(args, repo, loop_id, interactive)
        print("   init dry run:")
        if _verb(*argv, "--dry-run") != 0:
            print("\nsetup stopped: init refused the answers — fix what it names and re-run setup")
            return 2, admin
        if args.dry_run:
            print("\n3. would schedule the shared watchdog job (if missing)\n"
                  "4. would run doctor and selftest --no-model\n5. would offer to require the "
                  "diaktoros/review merge check, when the loop posts it\n6. would arm only when "
                  "asked")
            return 0, admin
        if not _agree("Install this loop?", True, interactive, True):
            print("setup stopped: nothing installed")
            return 1, admin
        if _verb(*argv) == 2:
            print("\nsetup stopped: init failed — see above; re-running setup is safe")
            return 2, admin
    return None, admin


def _setup_watchdog(args, loop_id: str, interactive: bool) -> tuple[int | None, bool]:
    """Step 3: the shared watchdog job, created only if missing. ``(rc, scheduled)``: rc is
    where setup stops (None: go on)."""
    print("\n3. The watchdog")
    schedule = args.schedule or _ask("how often the watchdog sweeps", "15m", interactive)
    deliver = args.watchdog_deliver or _ask("where its alerts go (local, telegram, …)", "local",
                                            interactive)
    if args.dry_run:
        print(f"   would schedule the shared watchdog job ({schedule}, deliver={deliver}) "
              "if it is missing")
        return 0, False
    lines, scheduled = cli_install._install_schedule(config.load_id(loop_id), schedule, deliver)
    for line in lines:
        print(f"   {'✅' if scheduled else '❌'} {line}")
    return None, scheduled


def _resume_rows(loop: dict) -> dict:
    """``onboarding.inspect``: every stage's status (``stale`` when its inputs changed) and why.
    No record, or one that cannot be read, resumes nothing: every stage reads
    ``not_attempted``, so every step runs."""
    try:
        return onboarding.inspect(loop)
    except Exception:          # noqa: BLE001 - an unreadable record only means: run the step
        return {}


def _status(rows: dict, stage: str) -> str:
    """``stage``'s status in ``rows``; ``passed`` only while its fingerprint still matches."""
    return (rows.get(stage) or {}).get("status", onboarding.NOT_ATTEMPTED)


# Step 4 is covered by the record when each of these stages holds one of these statuses
# (``stale`` is its own status, so a changed fingerprint never matches). ``selftest --no-model``
# records ``model`` as skipped, so skipped counts for it.
_CHECK_STAGES = {"identities": (onboarding.PASSED,), "loop": (onboarding.PASSED,),
                 "model": (onboarding.PASSED, onboarding.SKIPPED)}


def _first_uncovered(rows: dict) -> str:
    """The first of step 4's stages the record does not cover (not recorded, failed or stale),
    or ``""`` when it covers them all."""
    return next((stage for stage, ok in _CHECK_STAGES.items()
                 if _status(rows, stage) not in ok), "")


def _say_checks(rows: dict, first: str) -> None:
    """Say which of step 4's stages are skipped and why, or where it starts over and why."""
    if not first:
        print("   ⏭  skipped doctor and selftest --no-model: " + ", ".join(
            f"{stage} {_status(rows, stage)}" for stage in _CHECK_STAGES)
              + " in the onboarding record, and still current")
        return
    reason = (rows.get(first) or {}).get("reason")
    print(f"   starting at the {first} stage ({_status(rows, first)}"
          + (f": {reason}" if reason else "") + ")")


def _setup_checks(repo: str, loop_id: str, scheduled: bool,
                  runtime_ok: bool) -> int | None:
    """Step 4: ``doctor`` and ``selftest --no-model``, unless the onboarding record says they
    already passed and still hold (#1618); where setup stops on a ❌ (None: go on)."""
    print("\n4. Checks")
    rows = _resume_rows(config.load_id(loop_id))
    first = _first_uncovered(rows)
    if any(_status(rows, stage) != onboarding.NOT_ATTEMPTED for stage in onboarding.STAGES):
        _say_checks(rows, first)
    else:
        # ``inspect`` returns a row per stage even with no record, so ``rows`` alone never
        # tells a first install from a resume (#1712).
        print("   no onboarding record yet: running every check")
    doctor_rc = selftest_rc = 0
    if first:
        doctor_rc = _verb("doctor", f"--loop={loop_id}")
        selftest_rc = _verb("selftest", f"--loop={loop_id}", "--no-model")
    if doctor_rc or selftest_rc or not scheduled or not runtime_ok:
        print("\nsetup stopped before arming: fix each ❌ above (its fix line says how), then "
              f"re-run `hermes dk setup --repo {shlex.quote(repo)}` — it keeps what is done")
        _print_onboarding(loop_id)
        return 1
    return None


def _print_onboarding(loop_id: str) -> None:
    """The ``onboard`` summary, read-only, shared with ``cmd_onboard`` (``print_onboarding``).
    It never changes setup's result."""
    try:
        loop = config.load_id(loop_id)
        rows = onboarding.inspect(loop)
        print()
        cli_checks.print_onboarding(loop, rows)
    except Exception as exc:       # noqa: BLE001
        print(f"\n(onboarding summary unavailable: {type(exc).__name__}: {exc})")


def _setup_arm(args, loop_id: str, admin: str, interactive: bool) -> int:
    """Step 6: ``arm``, only when asked and not already recorded as armed and still current;
    then the ``onboard`` summary."""
    print("\n6. Arm")
    rc = _arm_step(args, loop_id, admin, interactive)
    _print_onboarding(loop_id)
    return rc


def _armed_current(rows: dict) -> bool:
    """Whether the record covers ``arm``: ``armed`` reads ``passed``, or it is stale only
    because ``live_turn`` (the stage before it) is stale while its own fingerprint, the hook
    state, still matches (#1713). A new PR head says nothing about the hooks; any other
    earlier stale stage, an invalidated entry or changed hooks still re-asks for arming."""
    row = rows.get("armed") or {}
    if _status(rows, "armed") == onboarding.PASSED:
        return True
    return (_status(rows, "armed") == onboarding.STALE and row.get("cause") == "live_turn"
            and row.get("own") == onboarding.PASSED)


def _arm_step(args, loop_id: str, admin: str, interactive: bool) -> int:
    rows = _resume_rows(config.load_id(loop_id))
    if _armed_current(rows):
        print("   ⏭  skipped arm: armed in the onboarding record, and the hooks are still "
              "as recorded")
        return 0
    if _status(rows, "armed") == onboarding.STALE:
        print(f"   starting at the armed stage (stale: {rows['armed']['reason']})")
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
    it yet, offer the loop's own ruleset; a loop that does not post it gets one line saying so
    (#1240: the steps never skip a number). Never blocks setup: the check is still posted, and
    doctor keeps saying the base does not require it."""
    from .. import protection
    if not config.merge_check_on(loop):
        print("\n5. The merge check — not posted by this loop, nothing to require")
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
