"""Read-only reporting commands: ``trace``, ``stats``, ``report`` and ``models`` (#676).

Each answers a question from the run ledger, a gate dry run, GitHub reads or Hermes's model
catalog, and writes nothing to the loop. ``cli`` re-exports every command here, so
``cli.cmd_stats`` and the parser's dispatch are unchanged.
"""
from __future__ import annotations

import json
import pathlib
import shlex

from .. import config, gh
from . import common as cli_common


def cmd_trace(args) -> int:
    """Dry-run one webhook through its gate: why it would, or would not, start a run (#216).

    The gate script runs for real on a temporary copy of the loop's home, so the answer is the
    live gate's own; nothing is posted, no run is started and the loop's state is untouched (see
    ``diaktoros.trace``). Exit 2 when it cannot be asked: an unknown loop, no such delivery, an
    unreadable payload file, or an event or route this loop does not serve.
    """
    from .. import trace
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    admin = getattr(args, "admin_token", "") or None
    refusal = cli_common.admin_unmapped(loop, admin)
    if refusal:
        print(f"refused: {refusal}")
        return 2
    try:
        if args.delivery:
            payload, event, route = trace.fetch_delivery(loop, args.delivery,
                                                         admin or loop.get("read_token"))
        else:
            try:
                payload = json.loads(pathlib.Path(args.payload).expanduser().read_text())
            except (OSError, ValueError) as exc:
                print(f"cannot read the payload file: {exc}")
                return 2
            if not isinstance(payload, dict):
                print("cannot read the payload file: not a JSON object")
                return 2
            event, route = args.event or trace.infer_event(payload), None
        role = trace.role_for(loop, event, args.route or route)
    except trace.TraceError as exc:
        print(f"cannot trace: {exc}")
        return 2
    return trace.run(loop, payload, event, role)


def cmd_stats(args) -> int:
    """What one loop did since ``--since``: seat turns from the run ledger (how they ended, how
    long they ran and waited) and, with ``--github``, its PRs and reviews read as the reader.

    Read-only: the ledger is opened read-only and GitHub is only read. ``--html FILE`` writes one
    self-contained page; ``--json`` prints the same data. Both hold totals and timings only, so
    either can be published (docs/operations.md, "Publishing stats").
    """
    from .. import run_supervisor, stats
    try:
        since = stats.parse_since(args.since)
    except ValueError as exc:
        print(str(exc))
        return 2
    if args.loop:
        try:
            loop = config.load_id(args.loop)
        except config.ConfigError as exc:
            print(f"no such loop: {exc}")
            return 2
    else:
        loops, refused = config.readable_loops()
        for loop_id, reason in refused:
            print(f"skipping {loop_id}.json: {reason}")
        names = [item["id"] for item in loops] + [loop_id for loop_id, _ in refused]
        if len(names) > 1:
            print(f"{len(names)} loops are configured ({', '.join(names)}) — name one with --loop")
            return 2
        if refused:
            return 2
        if not loops:
            print(f"no loops configured in {config.config_dir()}")
            return 2
        loop = loops[0]
    report = stats.collect(loop, run_supervisor.production_ledger(), since, args.github)
    print(stats.as_json(report) if args.json else stats.text(report))
    if args.html:
        target = pathlib.Path(args.html).expanduser()
        target.write_text(stats.as_html(report))
        if not args.json:
            print(f"\nwrote {target}")
    return 0


def cmd_report(args) -> int:
    """The weekly digest on demand, across every configured loop (or one with ``--loop``): PRs
    reviewed, verdicts, rounds, request-to-verdict time, escalations, rulings, finding and
    issue-fix outcomes, regressions after approval, turns and failures per seat and model, trends
    against the window before, and the most stuck PR. Read-only: the run ledger, the loops' breach
    markers and finding records, and one GitHub read per issue-fix PR."""
    from .. import run_supervisor, stats
    try:
        since = stats.parse_since(args.since)
    except ValueError as exc:
        print(str(exc))
        return 2
    if args.loop:
        try:
            loops = [config.load_id(args.loop)]
        except config.ConfigError as exc:
            print(f"no such loop: {exc}")
            return 2
    else:
        loops, refused = config.readable_loops()
        for loop_id, reason in refused:
            print(f"skipping {loop_id}.json: {reason}")
        if not loops:
            print(f"no loops configured in {config.config_dir()}")
            return 2
    db = run_supervisor.production_ledger()
    datas = [stats.report(loop, db, since) for loop in loops]
    print(json.dumps(datas, indent=2, sort_keys=True) if args.json
          else "\n\n".join(stats.report_text(data) for data in datas))
    return 0


def cmd_models(args) -> int:
    """Read-only: what a profile's provider offers, from Hermes's model catalog (issue #32).

    Changing a seat's model is changing that profile's model in Hermes (`hermes -p NAME model`);
    this only lists. Exit 1 when the profile, its provider or the catalog cannot answer.
    """
    from .. import seat_model
    from ..doctor import hook_checks, seat_checks
    if bool(args.profile) == bool(args.seat):
        print("pass exactly one of --profile-name NAME or --seat reviewer|fixer|adjudicator")
        return 2
    profile, seat = args.profile, args.seat
    if seat:
        try:
            loop = config.load_id(args.loop) if args.loop else None
            if loop is None:
                loops = config.all_loops()
                if not loops:
                    print(f"no loops configured in {config.config_dir()} — run "
                          "`hermes dk setup` to create one")
                    return 2
                if len(loops) > 1:
                    print("--seat needs --loop ID when more than one loop is configured")
                    return 2
                loop = loops[0]
        except config.ConfigError as exc:
            print(f"cannot read loop: {hook_checks._safe_report_text(str(exc))}")
            return 2
        profile = config.seat_profile(loop, seat)
    problem = seat_model.profile_problem(profile, seat or "requested")
    if problem:
        print(f"❌ {problem}")
        return 1
    settings, bad = seat_checks.runtime_settings()
    try:
        answer = seat_model.run_resolver(profile, "models", settings if bad is None else None)
    except seat_model.SeatModelError as exc:
        print(f"❌ {exc}")
        return 1
    provider = str(answer.get("requested") or "(auto)")
    current = str(answer.get("model") or "")
    print(f"profile {profile}{f' (seat {seat})' if seat else ''}: provider {provider}, "
          f"current model {current or '(unset)'}")
    if answer.get("error"):
        print(f"❌ {seat_model._redact(answer['error'])}")
        return 1
    rows = [(str(mid), "catalog") for mid in answer.get("catalog") or []]
    rows += [(str(mid), "profile config") for mid in answer.get("declared") or []
             if str(mid) not in {row[0] for row in rows}]
    if not answer.get("catalog_known"):
        print(f"⚠️  provider {provider} is unknown to the Hermes model catalog "
              "(no curated list; the catalog may also be unreachable or disabled)")
    if not rows:
        print("❌ no models listed for this provider — check the provider name, or ask the "
              "provider directly; nothing here changes the profile")
        return 1
    for mid, where in rows:
        print(f"  {'*' if mid == current else ' '} {mid}  ({where})")
    if current and current not in {mid for mid, _ in rows}:
        print(f"⚠️  the current model {current} is not in this list")
    print(f"change it with `hermes -p {profile} model` — this command never writes")
    return 0


# -- the parser for this module's commands (#676 step 7) --------------------------------------


def add_stats(sub) -> None:  # noqa: ANN001
    """The ``stats`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    stats_cmd = sub.add_parser("stats", help="What the loop did over a window: seat turns, "
                                             "how long they ran, and (with --github) its PRs "
                                             "and reviews")
    stats_cmd.add_argument("--loop", help="loop id (default: the only configured loop)")
    stats_cmd.add_argument("--since", default="7d",
                           help="window start: 7d, 24h or a date like 2026-09-28 (default 7d)")
    stats_cmd.add_argument("--github", action="store_true",
                           help="also read the window's PRs and reviews from GitHub, as the "
                                "reader (one request per PR)")
    stats_cmd.add_argument("--json", action="store_true", help="print the data as JSON")
    stats_cmd.add_argument("--html", metavar="FILE",
                           help="also write one self-contained HTML page to FILE")
    stats_cmd.set_defaults(func=cmd_stats)


def add_report(sub) -> None:  # noqa: ANN001
    """The ``report`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    report_cmd = sub.add_parser("report", help="The digest on demand, across all loops: reviews, "
                                               "rounds, time to verdict, escalations, rulings, "
                                               "finding and issue-fix outcomes, regressions "
                                               "after approval, turns per seat and model, "
                                               "trends, the most stuck PR")
    report_cmd.add_argument("--loop", help="one loop id (default: every configured loop)")
    report_cmd.add_argument("--since", default="7d",
                            help="window start: 7d, 24h or a date like 2026-09-28 (default 7d)")
    report_cmd.add_argument("--json", action="store_true", help="print the data as JSON")
    report_cmd.set_defaults(func=cmd_report)


def add_trace(sub) -> None:  # noqa: ANN001
    """The ``trace`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    tracer = sub.add_parser("trace", help="Dry-run one webhook through its gate: why it would "
                                          "(or would not) start a run")
    tracer.add_argument("--loop", required=True,
                        help="loop id (its config file name; `list` shows them)")
    source = tracer.add_mutually_exclusive_group(required=True)
    source.add_argument("--delivery", help="a recorded delivery to this loop's hooks: GitHub's "
                                           "numeric id or the X-GitHub-Delivery GUID")
    source.add_argument("--payload", help="a webhook payload JSON file instead")
    tracer.add_argument("--event", choices=("pull_request", "pull_request_review", "issues"),
                        help="with --payload: the event it was (default: read from the payload)")
    tracer.add_argument("--route", help="the route it was sent to (default: from the delivery's "
                                        "hook, or the event)")
    tracer.add_argument("--admin-token", default="",
                        help="login whose token can read hook deliveries (admin:repo_hook or repo)")
    tracer.set_defaults(func=cmd_trace)


def add_models(sub) -> None:  # noqa: ANN001
    """The ``models`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    models = sub.add_parser("models", help="Read-only: list the models a seat's Hermes "
                                           "profile's provider offers (Hermes catalog)")
    # Never `--profile`/`-p`: `hermes` takes those from anywhere on its command line to switch
    # its own profile, so the plugin would never see them.
    models.add_argument("--profile-name", dest="profile", help="Hermes profile name")
    models.add_argument("--seat", choices=("reviewer", "fixer", "adjudicator"),
                        help="use this seat's profile from the loop config")
    models.add_argument("--loop", help="loop id for --seat (default: the only loop)")
    models.set_defaults(func=cmd_models)
