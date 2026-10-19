"""Checks and arming (#676): ``doctor``, ``selftest``, ``corpus``, and ``arm``, which switches a
loop's GitHub hooks on or off and then verifies what it changed.

``cli`` re-exports every command here. ``arm_action`` owns the hook write and read-back
operation shared by ``arm`` and Desktop; it reuses the existing CLI hook plumbing.
"""
from __future__ import annotations

from .. import (
    arm_action,
    config,
    doctor,
    gate_shims,
    route_intent,
)


def cmd_doctor(args) -> int:
    """Read-only preflight: is this installation able to run the loop at all?

    Every check is in ``doctor.py`` — what each state means and, deliberately, everything this
    verb does *not* do (it writes nothing, and it never fires a route: a synthetic POST at a
    seat's route is a real agent run). This is the installation-level counterpart of the
    per-PR diagnosis in the watchdog, for the question `init` cannot answer about itself.
    """
    try:
        loops = [config.load_id(args.loop)] if args.loop else config.all_loops()
    except config.ConfigError as exc:
        print(f"cannot preflight loop: {doctor._safe_report_text(str(exc))}")
        return 2
    if not loops:
        print(f"no loops configured in {config.config_dir()} — run "
              "`hermes dk setup` to create one")
        return 0
    failed = 0
    for loop in loops:
        if getattr(args, "repair", False):
            # The one write doctor can make, and only when asked: put this loop's own routes back
            # from the plugin's intent record (same secret). Everything after it stays read-only.
            lines = route_intent.heal(loop) + gate_shims.heal(loop)
            print("\n".join(lines) if lines else
                  f"[{loop['id']}] repair: routes match the plugin's intent record — nothing restored")
        failed += doctor.report(loop, doctor.check_loop(loop, offline=args.offline),
                                strict=args.strict)
    return 1 if failed else 0


def cmd_selftest(args) -> int:
    """Verify the live isolated path for one loop; every step and its fix live in selftest.py.

    Unlike ``doctor`` this touches the real capabilities (bubblewrap, the model, GitHub reads, and
    with ``--live-turn`` one real reviewer turn) — but it only reads from GitHub unless ``--ping``
    is set: GitHub calls go through a GET-only guard and the live turn's broker runs in its
    host-only no-write mode. ``--ping`` is the one write: it asks GitHub to ping each loop hook.
    """
    from .. import selftest
    if args.live_turn and args.pr is None:
        print("--live-turn needs --pr N: the turn reviews that pull request")
        return 2
    if args.live_turn and args.no_model:
        print("--live-turn needs the model; drop --no-model")
        return 2
    if getattr(args, "timeout", None) is not None and args.timeout < 1:
        print("--timeout must be positive")
        return 2
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"cannot selftest loop: {doctor._safe_report_text(str(exc))}")
        return 2
    # Default to what production gives the reviewer seat, so a passing selftest proves the turn
    # fits the budget the unattended worker will actually enforce (#49).
    return selftest.run(loop, pr=args.pr, model=not args.no_model, live_turn=args.live_turn,
                        timeout=(args.timeout if getattr(args, "timeout", None) is not None
                                 else config.turn_budget(loop, "reviewer")),
                        ping=getattr(args, "ping", False),
                        ping_login=getattr(args, "admin_token", "") or None)


def cmd_corpus(args) -> int:
    """Replay the golden corpus through the reviewer (no-write) and record the scores (#491)."""
    from .. import (
        corpus,
        prompts,
        seat_model,
        selftest,
    )
    try:
        loop = config.load_id(args.loop)
        directory = corpus.corpus_dir(loop, args.dir)
        cases = corpus.load(directory)
        scores = config.state_dir(loop) / corpus.SCORES_FILE
    except (config.ConfigError, corpus.CorpusError) as exc:
        print(f"cannot run corpus: {doctor._safe_report_text(str(exc))}")
        return 2
    if args.history:
        bad: list = []
        for entry in corpus.history(scores, bad):
            print(f"{entry['prompt_rev']}  {entry['model']}  caught {entry['caught']}  "
                  f"missed {entry['missed']}")
        if bad:
            print(f"skipped {len(bad)} unreadable line(s) in {scores} "
                  f"(line {', '.join(map(str, bad))})")
        return 0
    if not cases:
        print(f"no cases in {directory}")
        return 2
    try:
        settings = selftest.check_runtime(selftest.Report(None, selftest.Redactor()),
                                          selftest.runtime_path())
        if settings is None:
            raise corpus.CorpusError("no usable runtime config; run `hermes dk selftest` first")
        reviewer = seat_model.resolve_seat(loop, "reviewer", settings)
        review = corpus.live_review(loop, settings, reviewer,
                                    args.timeout or config.turn_budget(loop, "reviewer"))
    except Exception as exc:
        print(f"cannot run corpus: {doctor._safe_report_text(str(exc))}")
        return 2
    try:
        with selftest.github_read_only(), selftest.worker_tempdir():
            corpus.check_heads(loop, cases)
            results = corpus.replay(cases, review)
    except corpus.CorpusError as exc:
        print(f"cannot run corpus: {doctor._safe_report_text(str(exc))}")
        return 2
    for r in results:
        print(f"{r['case']}: caught {len(r['caught'])} missed {len(r['missed'])}"
              + (f" ({', '.join(r['missed'])})" if r["missed"] else "")
              + (f" [{r['error']}]" if r.get("error") else ""))
    entry = corpus.record(scores, prompts.revision("reviewer"), reviewer.model, results)
    print(f"caught {entry['caught']}, missed {entry['missed']} "
          f"(prompt {entry['prompt_rev']}, model {entry['model']}); recorded in {scores}")
    return 1 if entry["missed"] else 0


def cmd_arm(args) -> int:
    """Arm or pause loops by flipping their repo hooks; exit 1 unless GitHub confirms every one."""
    try:
        loops = [config.load_id(args.loop)] if args.loop else config.all_loops()
    except config.ConfigError as exc:
        print(f"cannot {'pause' if args.pause else 'arm'}: {exc}")
        return 2
    if not loops:
        print(f"no loops configured in {config.config_dir()} — nothing to "
              f"{'pause' if args.pause else 'arm'}")
        return 2
    failed = []
    for loop in loops:
        lines, ok = arm_action.perform(loop, not args.pause, args.admin_token)
        for line in lines:
            print(f"[{loop['id']}] {line}")
        if not ok:
            failed.append(loop["id"])
    if failed:
        print(f"{'pause' if args.pause else 'arm'} NOT confirmed for: {', '.join(failed)} "
              "— the lines above show what GitHub reports now")
        return 1
    return 0


# -- the parser for this module's commands (#676 step 7) --------------------------------------


def add_doctor(sub) -> None:  # noqa: ANN001
    """The ``doctor`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    preflight = sub.add_parser("doctor", help="Preflight a loop read-only: profiles, tokens, "
                                              "routes, hooks, scripts, cron, clone")
    preflight.add_argument("--loop", help="loop id (default: every configured loop)")
    preflight.add_argument("--offline", action="store_true",
                           help="skip every network probe, each reported as not probed: gateway "
                                "reachability, repo hooks, review-only push access, token expiry "
                                "and scopes, triage labels, branch protection, plugin version")
    preflight.add_argument("--strict", action="store_true",
                           help="treat a check that could not be decided as a failure")
    preflight.add_argument("--repair", action="store_true",
                           help="restore this loop's own routes from the plugin's intent record "
                                "(same secret) before checking; the only write doctor makes")
    preflight.set_defaults(func=cmd_doctor)


def add_selftest(sub) -> None:  # noqa: ANN001
    """The ``selftest`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    check = sub.add_parser("selftest", help="Verify the live isolated path step by step "
                                            "(runtime, bwrap, model, identities, broker, ledger); "
                                            "reads only from GitHub unless --ping is set")
    check.add_argument("--loop", required=True,
                       help="loop id (its config file name; `list` shows them)")
    check.add_argument("--pr", type=int, help="dry-run the reviewer write authorization on this PR")
    check.add_argument("--no-model", action="store_true",
                       help="skip the one tiny real completion (costs a few tokens)")
    check.add_argument("--live-turn", action="store_true",
                       help="with --pr: run one real isolated reviewer turn whose verdict is "
                            "printed and never posted")
    check.add_argument("--ping", action="store_true",
                       help="ask GitHub to ping each loop hook and report whether the gateway "
                            "accepted its signature (the selftest's only GitHub write)")
    check.add_argument("--admin-token", default="",
                       help="login whose token may ping hooks (admin:repo_hook or repo)")
    check.add_argument("--timeout", type=int, default=None,
                       help="live turn budget in seconds (default: the loop's reviewer "
                            "turn_budget_s — the budget the production worker enforces)")
    check.set_defaults(func=cmd_selftest)


def add_corpus(sub) -> None:  # noqa: ANN001
    """The ``corpus`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    corpus_cmd = sub.add_parser("corpus", help="Replay historical PRs with known problems "
                                               "through the reviewer (no-write) and score "
                                               "what it caught, per prompt revision and model")
    corpus_cmd.add_argument("--loop", required=True, help="loop id")
    corpus_cmd.add_argument("--dir", default="",
                            help="directory of case files (default: corpus/ in the loop's "
                                 "state directory)")
    corpus_cmd.add_argument("--timeout", type=int, default=None,
                            help="per-case turn budget in seconds (default: the reviewer's)")
    corpus_cmd.add_argument("--history", action="store_true",
                            help="print the recorded scores and run nothing")
    corpus_cmd.set_defaults(func=cmd_corpus)


def add_arm(sub) -> None:  # noqa: ANN001
    """The ``arm`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    arm = sub.add_parser("arm", help="Activate the loop's GitHub hooks")
    arm.add_argument("--loop", help="loop id (default: every configured loop)")
    arm.add_argument("--pause", action="store_true", help="pause instead of arming")
    arm.add_argument("--admin-token", default="",
                     help="login whose token can edit the repo's hooks (default: the reader)")
    arm.set_defaults(func=cmd_arm)
