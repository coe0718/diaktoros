"""``hermes dk`` — install, inspect and drive the loop from the CLI.

The plugin does not try to own the gateway. Everything the loop needs beyond its own scripts —
webhook routes, GitHub hooks, a cron entry — is written through the same config surfaces the
operator would touch by hand, so `hermes dk uninstall` is really just the inverse of
`init` and nothing lives in a place you cannot see.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile

from .ledger import LOCK_WAIT_S
from . import (
    attribution,
    config,
    doctor,
    envnames,
    gate,
    gate_shims,
    gh,
    observer,
    pr_hold,
    prompts,
    route_intent,
    routes,
    state as state_mod,
)
from .util import logged
# Commands that live in their own modules (#676), re-exported so `cli.cmd_*` and the parser's
# dispatch keep working: the implementation is there, never a second copy here.
from . import cli_common
from .cli_common import (  # noqa: F401 - constants, re-exported for doctor, backup, gate_shims
    BRANCH_PUSH_REFUSAL, GATE_SCRIPT, SHARED_JOB_NAME, SHARED_JOB_NAMES, SHIM, SHIM_NAME,
    _ROUTE_PROMPT, _hook_route_name,
)
from .cli_checks import cmd_arm, cmd_corpus, cmd_doctor, cmd_selftest  # noqa: F401
from .cli_install import cmd_init, cmd_setup  # noqa: F401
from .cli_lifecycle import cmd_backup, cmd_migrate, cmd_restore, cmd_uninstall  # noqa: F401
from .cli_operations import (  # noqa: F401
    cmd_cleanup, cmd_depends, cmd_drain, cmd_escalate, cmd_explain, cmd_list, cmd_ready,
    cmd_regression, cmd_retry, cmd_review, cmd_status,
)
from .cli_reports import cmd_models, cmd_report, cmd_stats, cmd_trace  # noqa: F401
from .cli_settings import (  # noqa: F401
    cmd_apply, cmd_fixer_push, cmd_set, cmd_settings, cmd_show, cmd_triage,
)

# -- verbs ----------------------------------------------------------------------


RENAMED_NOTE = ("note: `hermes review-loop` is now `hermes dk` (short for `hermes diaktoros`); "
                "the old name will be removed in a later release")


def _renamed(func):
    """``func`` run through the old command name: the rename note on stderr, then the verb."""
    def run(args):  # noqa: ANN001
        print(RENAMED_NOTE, file=sys.stderr)
        return func(args)
    return run


def register_cli(ctx, settings: dict | None = None) -> None:
    """Wire ``hermes diaktoros``, its short name ``hermes dk``, and the old ``hermes review-loop``.

    ``settings`` is the plugin-level settings form. It supplies the defaults a new loop starts
    from, and what ``apply`` pushes onto an existing loop; it never rewrites a loop behind the
    operator's back.
    """
    cli_common._SETTINGS = dict(settings or {})
    cli_common._CTX = ctx
    d = config.settings_defaults(settings)
    # Both seats agreeing is the only case where a loop-level default says anything useful: when
    # they disagree the differing seat carries its value explicitly (see _init_seat_concurrency).
    both = config.settings_loop_concurrency(d)

    def setup(parser) -> None:  # noqa: ANN001
        """Build the command's argparse tree.

        The framework hands this the parser for ``hermes dk`` itself — the subcommands are
        ours to create. (Claiming a subparsers action here compiles, loads, validates, and then
        quietly offers zero subcommands: ``hermes dk list`` is "unrecognized arguments".)
        """
        sub = parser.add_subparsers(dest="command", metavar="<command>")
        cli_common._PARSER = parser

        def _usage(_args) -> int:      # bare `hermes dk` prints the commands, not an error
            parser.print_help()
            return 0

        parser.set_defaults(func=_usage)
        sub.add_parser("list", help="List configured loops").set_defaults(func=cmd_list)

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
                               "from the plugin settings). Above 1 needs --clone, because each "
                               "run then gets its own clone. Override per seat with "
                               "--reviewer-concurrency / --fixer-concurrency.")
        init.add_argument("--reviewer-concurrency", type=int, default=None,
                          help="PRs the reviewer may work at once (overrides --concurrency; "
                               f"default: the loop's, settings {d['reviewer_concurrency']})")
        init.add_argument("--fixer-concurrency", type=int, default=None,
                          help="PRs the fixer may work at once (overrides --concurrency; "
                               f"default: the loop's, settings {d['fixer_concurrency']})")
        init.add_argument("--base", default=d["base"],
                          help="the branch PRs target; only PRs against it are reviewed")
        init.add_argument("--clone", default=d["clone"], help="local clone the runs may use")
        init.add_argument("--root", action="append", default=[], help="a directory reviews may clean (repeatable)")
        init.add_argument("--state-dir", default="",
                          help="where this loop keeps its state files (default: "
                               "~/.hermes/state/review-loops/<id>)")
        init.add_argument("--token", action="append", default=[], help="login=/path/to/pat (repeatable)")
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
        init.add_argument("--review-after-ci", choices=("on", "off"), default=None,
                          help="start each review after the head's checks finish (up to an hour) "
                               f"(default {'on' if d['review_after_ci'] else 'off'})")
        init.add_argument("--fix-ci", choices=("on", "off"), default=None,
                          help="hand a failed required check on a fixer's PR to the fixer, one "
                               "turn per head; needs unattended fixer pushes "
                               f"(default {'on' if d['fix_ci'] else 'off'})")
        init.add_argument("--attribution", choices=("on", "off"), default=None,
                          help="sign what the loop posts with 'Automated by Diaktoros' "
                               f"(default {'on' if d['attribution'] else 'off'})")
        init.add_argument("--review-only-cap", default=None, metavar="N",
                          help="verdicts the reviewer gives one review-only PR before it waits "
                               "for `review --another-round`, 1-1000 (default: the plugin "
                               "setting, else the review cap)")
        init.add_argument("--ci-fix-cap", default=None, metavar="N",
                          help="CI-fix turns per PR before a red head goes to the reviewer, 1-10 "
                               "(default: the plugin setting, else 3)")
        init.add_argument("--review-only-daily", default=None, metavar="N",
                          help="reviewer turns a day on review-only PRs, 1-1000 (default: the "
                               "plugin setting, else no cap)")
        init.add_argument("--review-only", action="append", default=None,
                          help="a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it, or comma-separated) (default: the plugin setting)")
        init.add_argument("--required-check", action="append", default=None,
                          help="a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates) (default: the plugin setting)")
        init.add_argument("--human-path", action="append", default=None,
                          help="a glob pattern for paths only a human may approve: the loop's approval of a diff touching one is left to a person (repeat it; none = no path reserved) (default: the plugin setting)")
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
        init.add_argument("--fixer-daily-tokens", type=int, default=None,
                          help="most tokens the fixer (and issue fixes) may spend per day "
                               "(0 = none) (default: the plugin setting)")
        init.add_argument("--fix-daily-turns", type=int, default=None,
                          help="issue-fix turns per day: refused here, a new loop has no fix "
                               "label; set it with `triage --fix-daily-turns N` (0 = ignore)")
        init.add_argument("--hooks", action="store_true",
                          help="create the GitHub hooks too, paused until `arm`")
        init.add_argument("--arm", action="store_true",
                          help="with --hooks: create them armed (live at once) instead of paused")
        init.add_argument("--admin-token", default="", help="login whose token can create hooks")
        init.add_argument("--schedule", default="", help="e.g. 15m — install the watchdog cron job")
        init.add_argument("--watchdog-deliver", default="local", help="cron delivery target for watchdog alerts")
        init.add_argument("--dry-run", action="store_true",
                          help="print the seat mapping and what would be written, write nothing")
        init.set_defaults(func=cmd_init)

        first = sub.add_parser("setup", help="First install in one command: runtime paths, init, "
                                             "watchdog, doctor, selftest, then arm when asked "
                                             "(safe to re-run)")
        first.add_argument("--repo", help="owner/name (asked when not given)")
        first.add_argument("--id", help="loop id (default: the repository name)")
        first.add_argument("--yes", action="store_true",
                           help="no questions: the flags and the plugin settings are the answers, "
                                "and every confirmation is yes (arming still needs --arm)")
        first.add_argument("--dry-run", action="store_true", help="show every step, write nothing")
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
                           help="CI-fix turns per PR before a red head goes to the reviewer, 1-10 "
                                "(default: the plugin setting, else 3)")
        first.add_argument("--review-only-daily", default=None, metavar="N",
                           help="reviewer turns a day on review-only PRs, 1-1000 (default: the "
                                "plugin setting, else no cap)")
        first.add_argument("--review-only", action="append", default=None,
                           help="a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it, or comma-separated) (default: the plugin setting)")
        first.add_argument("--required-check", action="append", default=None,
                           help="a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates) (default: the plugin setting)")
        first.add_argument("--human-path", action="append", default=None,
                           help="a glob pattern for paths only a human may approve: the loop's approval of a diff touching one is left to a person (repeat it; none = no path reserved) (default: the plugin setting)")
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

        status = sub.add_parser("status", help="Show a loop's config and live state")
        status.add_argument("--loop", help="loop id (default: every configured loop)")
        status.set_defaults(func=cmd_status)

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

        review = sub.add_parser("review", help="Ask for a fresh review of a PR's current head "
                                               "(the reviewer gate decides, as for a webhook)")
        review.add_argument("--loop", help="loop id (default: the only configured loop)")
        review.add_argument("--pr", type=int, required=True, help="the pull request to review")
        review.add_argument("--another-round", action="store_true",
                            help="allow exactly one more verdict on a review-only PR that has "
                                 "reached its review cap (maintainer or operator only)")
        review.set_defaults(func=cmd_review)

        escalate = sub.add_parser("escalate", help="Send one PR to the adjudicator now, whatever "
                                                   "its verdict count (#459)")
        escalate.add_argument("--loop", help="loop id (default: the only configured loop)")
        escalate.add_argument("--pr", type=int, required=True, help="the pull request to escalate")
        escalate.add_argument("--reason", default="", help="why, for the ruling's record (one line)")
        escalate.set_defaults(func=cmd_escalate)

        depends = sub.add_parser("depends",
                                 help="Make one issue's fix wait for another issue's merged "
                                      "implementation (#671), waive it, or list them")
        depends.add_argument("--loop", help="loop id (default: the only configured loop)")
        depends.add_argument("--issue", type=int, default=None,
                             help="the issue that waits (with --on/--waive; alone: its list)")
        depends_how = depends.add_mutually_exclusive_group()
        depends_how.add_argument("--on", type=int, default=None,
                                 help="the prerequisite issue whose implementation must merge")
        depends_how.add_argument("--waive", type=int, default=None,
                                 help="remove this prerequisite from --issue")
        depends.set_defaults(func=cmd_depends)

        regression = sub.add_parser("regression",
                                    help="Link a later bug to the approved PR that shipped it, "
                                         "for the report (#661); checked against GitHub")
        regression.add_argument("--loop", help="loop id (default: the only configured loop)")
        regression.add_argument("--issue", type=int, required=True,
                                help="the bug report (an issue opened after the PR merged)")
        regression.add_argument("--pr", type=int, required=True,
                                help="the merged pull request that introduced it")
        regression.add_argument("--retract", action="store_true",
                                help="withdraw this link (kept as retracted)")
        regression.set_defaults(func=cmd_regression)

        explain = sub.add_parser("explain",
                                 help="Why one PR is not moving, and what has to happen next")
        explain.add_argument("--loop", help="loop id (default: the only configured loop)")
        explain.add_argument("--pr", type=int, required=True, help="pull request number to explain")
        explain.set_defaults(func=cmd_explain)

        ready = sub.add_parser("ready", help="List the open PRs that are safe to merge, in merge "
                                             "order, and why the others are not (read-only)")
        ready.add_argument("--loop", help="loop id (default: the only configured loop)")
        ready.set_defaults(func=cmd_ready)

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

        move = sub.add_parser("migrate", help="Move an install from the hermes-review-loop plugin to "
                                              "this one: settings, a renamed repository, shims (#425)")
        move.add_argument("--dry-run", action="store_true",
                          help="report every step and write nothing")
        move.add_argument("--rename-loop", metavar="OLD=NEW", default=None,
                          help="also give a loop a new id: its file, default state directory, "
                               "routes and the URLs its repo hooks post to (each hook is pinged "
                               "before the old route goes)")
        move.add_argument("--admin-token", default=None, metavar="LOGIN",
                          help="mapped login whose token may edit the repo hooks (--rename-loop)")
        move.set_defaults(func=cmd_migrate)

        bak = sub.add_parser("backup", help="Write one 0600 archive of everything the plugin owns "
                                            "(loops, ledger, state, routes, watchdog job) (#496)")
        bak.add_argument("--out", metavar="FILE", default=None,
                         help="where to write it (default: $HERMES_HOME/backups/…); never "
                              "overwrites a file")
        bak.set_defaults(func=cmd_backup)

        rest = sub.add_parser("restore", help="Put a backup back: files, routes, shims, the "
                                              "watchdog job; ends with doctor (#496)")
        rest.add_argument("file", metavar="FILE", help="an archive `backup` wrote")
        rest.add_argument("--dry-run", action="store_true",
                          help="report what would be restored and overwritten; write nothing")
        rest.add_argument("--force", action="store_true",
                          help="overwrite existing state (refused without it)")
        rest.add_argument("--allow-state-dir", action="append", default=None, metavar="DIR",
                          help="write this loop state directory, which the archive declares "
                               "outside the Hermes home (the dry run lists them; repeat per "
                               "directory)")
        rest.set_defaults(func=cmd_restore)

        preflight = sub.add_parser("doctor", help="Preflight a loop read-only: profiles, tokens, "
                                                  "routes, hooks, scripts, cron, clone")
        preflight.add_argument("--loop", help="loop id (default: every configured loop)")
        preflight.add_argument("--offline", action="store_true",
                               help="skip the two network probes (gateway reachability, repo hooks)")
        preflight.add_argument("--strict", action="store_true",
                               help="treat a check that could not be decided as a failure")
        preflight.add_argument("--repair", action="store_true",
                               help="restore this loop's own routes from the plugin's intent record "
                                    "(same secret) before checking; the only write doctor makes")
        preflight.set_defaults(func=cmd_doctor)

        check = sub.add_parser("selftest", help="Verify the live isolated path step by step "
                                                "(runtime, bwrap, model, identities, broker, ledger); "
                                                "never writes to GitHub")
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

        models = sub.add_parser("models", help="Read-only: list the models a seat's Hermes "
                                               "profile's provider offers (Hermes catalog)")
        # Never `--profile`/`-p`: `hermes` takes those from anywhere on its command line to switch
        # its own profile, so the plugin would never see them.
        models.add_argument("--profile-name", dest="profile", help="Hermes profile name")
        models.add_argument("--seat", choices=("reviewer", "fixer", "adjudicator"),
                            help="use this seat's profile from the loop config")
        models.add_argument("--loop", help="loop id for --seat (default: the only loop)")
        models.set_defaults(func=cmd_models)

        change = sub.add_parser("set", help="Change a loop's settings in place")
        change.add_argument("--loop", required=True,
                            help="loop id (its config file name; `list` shows them)")
        change.add_argument("--concurrency", type=int,
                            help="default PRs per seat at once (1 = serialized; above 1 needs a "
                                 "clone, since each run gets its own)")
        change.add_argument("--reviewer-concurrency", type=int, default=None,
                            help="how many PRs the reviewer may work at once")
        change.add_argument("--fixer-concurrency", type=int, default=None,
                            help="how many PRs the fixer may work at once")
        change.add_argument("--cap", type=int, help="verdicts allowed before adjudication")
        change.add_argument("--clone", help="local clone the runs isolate from")
        change.add_argument("--base", help="base branch the loop watches")
        change.add_argument("--grace-min", type=int, help="quiet minutes before the watchdog speaks")
        change.add_argument("--marker-grace-min", type=int,
                            help="minutes an adjudication may sit claimed with no live run before "
                                 "the watchdog reports it")
        change.add_argument("--ttl-min", type=int, help="how long a run may hold its slot")
        change.add_argument("--inflight-ttl-min", type=int,
                            help="minutes an in-flight mark blocks a second run at the same head")
        change.add_argument("--turn-budget", type=int,
                            help="seconds one isolated seat turn may run (loop default)")
        change.add_argument("--attribution", choices=("on", "off"), default=None,
                            help="sign what the loop posts ('Automated by Diaktoros'), "
                                 "or stop")
        change.add_argument("--review-only-update", choices=("on", "off"), default=None,
                            help="push a clean merge of the base into a review-only author's PR "
                                 "branch (same repository only), or stop; turning it on needs "
                                 "--acknowledge-branch-push")
        change.add_argument("--acknowledge-branch-push", action="store_true",
                            help="accept that the host pushes to a branch the loop does not own; "
                                 "required to turn --review-only-update on")
        change.add_argument("--review-after-ci", choices=("on", "off"), default=None,
                            help="start each review after the head's checks finish (up to an hour), "
                                 "or start at once")
        change.add_argument("--fix-ci", choices=("on", "off"), default=None,
                            help="hand a failed required check on a fixer's PR to the fixer "
                                 "(needs unattended fixer pushes), or stop")
        change.add_argument("--review-only-cap", default=None, metavar="N",
                            help="verdicts the reviewer gives one review-only PR before it waits "
                                 "for `review --another-round`, 1-1000; 0 = the review cap")
        change.add_argument("--ci-fix-cap", default=None, metavar="N",
                            help="CI-fix turns per PR before a red head goes to the reviewer, "
                                 "1-10; 0 = the default (3)")
        change.add_argument("--review-only-daily", default=None, metavar="N",
                            help="reviewer turns a day on review-only PRs, 1-1000; 0 = no cap")
        reviewed = change.add_mutually_exclusive_group()
        reviewed.add_argument("--review-only", action="append", default=None,
                              help="a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it, or comma-separated); replaces the list")
        reviewed.add_argument("--no-review-only", action="store_true",
                              help="clear the review-only list")
        required = change.add_mutually_exclusive_group()
        required.add_argument("--required-check", action="append", default=None,
                              help="a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates); replaces the list")
        required.add_argument("--no-required-checks", action="store_true",
                              help="clear the list: every check gates again")
        humans = change.add_mutually_exclusive_group()
        humans.add_argument("--human-path", action="append", default=None,
                            help="a glob pattern for paths only a human may approve (repeat it); replaces the list")
        humans.add_argument("--no-human-paths", action="store_true",
                            help="clear the list: no path is reserved for a human")
        change.add_argument("--fixer-check", default=None, help="one command the fixer runs before every push or issue-fix PR, besides its touched tests (chain several with &&; '' for none)")
        change.add_argument("--reviewer-turn-budget", type=int, default=None,
                            help="the reviewer seat's own turn budget in seconds")
        change.add_argument("--fixer-turn-budget", type=int, default=None,
                            help="the fixer seat's own turn budget in seconds")
        change.add_argument("--reviewer-daily-turns", type=int, default=None,
                            help="most reviewer turns per day on this loop; later ones wait for "
                                 "midnight (0 removes the cap)")
        change.add_argument("--fixer-daily-turns", type=int, default=None,
                            help="most fixer turns per day on this loop (0 removes the cap)")
        change.add_argument("--reviewer-daily-tokens", type=int, default=None,
                            help="most tokens (in + out) the reviewer may spend per day on this "
                                 "loop; once spent, the next turn waits for midnight (0 removes it)")
        change.add_argument("--fixer-daily-tokens", type=int, default=None,
                            help="most tokens the fixer (and issue fixes) may spend per day on "
                                 "this loop (0 removes it)")
        change.add_argument("--reviewer-max-steps", type=int, default=None,
                            help="agent steps one reviewer turn may take, 8-200 (0 = default 60)")
        change.add_argument("--fixer-max-steps", type=int, default=None,
                            help="agent steps one fixer or issue-fix turn may take, 8-200 "
                                 "(0 = default 80)")
        change.add_argument("--host", help="gateway webhook host")
        change.add_argument("--adjudicator-login", default=None,
                            help="optional fourth GitHub account the ruling is also posted as; "
                                 "\"\" clears it (rulings go to the operator only)")
        change.add_argument("--adjudicator-profile", default=None,
                            help="turn adjudication on: the Hermes profile that rules when the "
                                 "verdict cap is spent; creates the <id>-breach route and its shim")
        change.add_argument("--adjudicator-route", default=None,
                            help="with --adjudicator-profile: the route's name (default: <id>-breach)")
        change.add_argument("--adjudicator", choices=("off",), default=None,
                            help="turn adjudication off: removes the route and the block (breach "
                                 "markers and rulings stay in the ledger)")
        change.add_argument("--read-token", default=None,
                            help="the login the gates read GitHub as — its own account, never a "
                                 "seat or the adjudicator login (the four-identity rule); map a "
                                 "new login with --token LOGIN=/path")
        change.add_argument("--token", action="append", default=[],
                            help="LOGIN=/path/to/pat for the --read-token or --adjudicator-login "
                                 "login only (a path, never the token)")
        change.add_argument("--observer-route", help="route the observer feed delivers through")
        change.add_argument("--observer-profile", help="profile that owns the observer destination")
        change.add_argument("--observer-deliver",
                            help="where the gateway delivers the feed (telegram, discord, ...)")
        change.add_argument("--observer-events", default=None,
                            help="comma-separated transitions to send, from "
                                 "opened,handoff,verdict,approved,escalation,ruling,stall,closed,"
                                 "triaged,fixing,fixed,failed,held,conflict,ci_failed,updated,main_red,stale_approval,human_paths,ready_set "
                                 "(blank = all)")
        change.add_argument("--observer-digest-min", type=int, default=None,
                            help="batch the feed into one message per N minutes (0 = per "
                                 "transition)")
        change.add_argument("--observer-weekly-report", default=None,
                            help="the week's report once a week at a weekday and UTC time like "
                                 "'mon 09:00' (blank = none)")
        change.add_argument("--observer-urgent-route", default=None,
                            help="route for urgent notices only (blank = one feed for everything)")
        change.add_argument("--observer-urgent-profile", default=None,
                            help="profile that owns the urgent destination (blank = the feed's)")
        change.add_argument("--observer-urgent-deliver", default=None,
                            help="where the gateway delivers urgent notices (blank = the feed's)")
        change.add_argument("--observer-mute", action="store_true",
                            help="stop the feed without forgetting it")
        change.add_argument("--observer-unmute", action="store_true", help="resume a muted feed")
        change.add_argument("--observer-disable", action="store_true",
                            help="drop this loop's observer config entirely")
        change.set_defaults(func=cmd_set)

        apply_cmd = sub.add_parser("apply", help="Push the plugin settings onto a loop")
        apply_cmd.add_argument("--loop", required=True,
                               help="loop id (its config file name; `list` shows them)")
        apply_cmd.add_argument("--dry-run", action="store_true",
                               help="show the diff without writing it")
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
                               help="make this loop's two repo hooks what its routes need: "
                                    "create a missing one (paused until arm), repoint one at the "
                                    "route's exact URL, add its gate's event (hook write access, "
                                    "see --admin-token)")
        apply_cmd.add_argument("--watchdog-shim", action="store_true",
                               help="rewrite the cron shim the watchdog job runs, pinned to this "
                                    "plugin's watchdog")
        apply_cmd.set_defaults(func=cmd_apply)

        settings_cmd = sub.add_parser("settings",
                                      help="Show the plugin-level defaults a loop starts from")
        settings_cmd.set_defaults(func=cmd_settings)

        show_cmd = sub.add_parser("show",
                                  help="Every setting of one loop: value, source, meaning")
        show_cmd.add_argument("--loop", required=True,
                              help="loop id (its config file name; `list` shows them)")
        show_cmd.add_argument("--json", action="store_true", help="machine-readable output")
        show_cmd.set_defaults(func=cmd_show)

        arm = sub.add_parser("arm", help="Activate the loop's GitHub hooks")
        arm.add_argument("--loop", help="loop id (default: every configured loop)")
        arm.add_argument("--pause", action="store_true", help="pause instead of arming")
        arm.add_argument("--admin-token", default="",
                         help="login whose token can edit the repo's hooks (default: the reader)")
        arm.set_defaults(func=cmd_arm)

        triage = sub.add_parser("triage", help="Issue triage for one loop: --enable, --disable, "
                                               "or show it (#213)")
        triage.add_argument("--loop", required=True,
                            help="loop id (its config file name; `list` shows them)")
        switch = triage.add_mutually_exclusive_group()
        switch.add_argument("--enable", action="store_true",
                            help="turn triage on (or change it): writes its route, shim and, with "
                                 "--admin-token, its issues hook (paused until arm)")
        switch.add_argument("--disable", action="store_true",
                            help="turn triage off: removes its route, shim and (with --admin-token) hook")
        triage.add_argument("--triage-profile", dest="profile", default="",
                            help="Hermes profile whose model triages")
        triage.add_argument("--author", action="append", default=[],
                            help="GitHub login whose new issues are triaged (repeatable); "
                                 "anyone else's are ignored")
        triage.add_argument("--labels", default="",
                            help="comma-separated labels triage may apply, e.g. "
                                 "bug,feature,docs,question,P0,P1,P2,P3")
        triage.add_argument("--max-labels", type=int, default=None,
                            help="at most this many labels per issue (default 3)")
        triage.add_argument("--comment", choices=("on", "off"), default=None,
                            help="allow one short comment with the labels (default off)")
        triage.add_argument("--login", default="",
                            help="account that labels (default: the reviewer seat); needs "
                                 "issues: write, never the reader")
        triage.add_argument("--token", action="append", default=[],
                            help="login=/path/to/pat for --login (or --admin-token), if not mapped")
        triage.add_argument("--fix-label", default=None,
                            help="a label a maintainer applies to hand an issue to the fixer "
                                 "(#214; needs unattended fixer pushes on); '' turns it off")
        triage.add_argument("--maintainer", action="append", default=[],
                            help="login whose applying --fix-label counts (repeatable)")
        triage.add_argument("--daily-turns", type=int, default=None,
                            help="at most this many triage turns per day (0 removes the cap)")
        triage.add_argument("--fix-daily-turns", type=int, default=None,
                            help="at most this many issue-fix turns per day (0 = the default, "
                                 f"{config.DEFAULT_FIX_DAILY_TURNS}); issue fixes are always "
                                 "capped")
        triage.add_argument("--auto-fix-label", action="append", default=None,
                            help="a triage label that hands an issue to the fixer without a "
                                 "maintainer (#232; repeatable; never P0-P2; '' clears the list)")
        triage.add_argument("--auto-fix-daily", type=int, default=None,
                            help="at most this many automatic issue fixes per day (0 = the "
                                 f"default, {config.DEFAULT_AUTO_FIX_DAILY})")
        triage.add_argument("--admin-token", default="",
                            help="login whose token can create or delete repo hooks")
        triage.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
        triage.set_defaults(func=cmd_triage)

        fixer_push = sub.add_parser("fixer-push", help="Explicit per-repository unattended fixer push policy")
        fixer_push.add_argument("--loop", required=True, help="exact loop id (never all loops)")
        direction = fixer_push.add_mutually_exclusive_group(required=True)
        direction.add_argument("--enable", action="store_true", help="opt this repository in")
        direction.add_argument("--disable", action="store_true", help="turn unattended pushes off")
        fixer_push.add_argument("--acknowledge-pr-race", action="store_true",
                                help="accept the residual non-atomic PR-metadata/ref race; required for --enable")
        fixer_push.add_argument("--dry-run", action="store_true", help="show action without writing")
        fixer_push.set_defaults(func=cmd_fixer_push)

        retry = sub.add_parser("retry", help="Re-arm a PR's isolated run that failed before "
                                             "any GitHub write (refuses one that may have written)")
        retry.add_argument("--loop", required=True,
                           help="loop id (its config file name; `list` shows them)")
        retry.add_argument("--pr", type=int, required=True,
                           help="the pull request whose failed run to re-arm")
        retry.add_argument("--seat", choices=["reviewer", "fixer", "adjudicator", "triage",
                                              "issue_fixer"],
                           help="only that seat's run (default: whichever failed at the PR's "
                                "newest head)")
        retry.set_defaults(func=cmd_retry)

        drain = sub.add_parser("drain", help="Start a queued run once its seat is free")
        drain.add_argument("--loop", required=True,
                           help="loop id (its config file name; `list` shows them)")
        drain.add_argument("--seat", default="reviewer", choices=["reviewer", "fixer"],
                           help="which seat's queue to drain")
        drain.set_defaults(func=cmd_drain)

        cleanup = sub.add_parser("cleanup", help="Reclaim local disk for finished PRs")
        cleanup.add_argument("--loop", required=True,
                             help="loop id (its config file name; `list` shows them)")
        cleanup.add_argument("--pr", type=int,
                             help="clean one closed PR (default: sweep every closed PR the clone "
                                  "knows about)")
        cleanup.add_argument("--dry-run", action="store_true",
                             help="list what would be removed, remove nothing")
        cleanup.set_defaults(func=cmd_cleanup)

        uninstall = sub.add_parser("uninstall", help="Remove a loop: its repo hooks, cron job, "
                                   "routes and config (refuses rather than leave live hooks)")
        uninstall.add_argument("--loop", required=True,
                               help="loop id (its config file name; `list` shows them)")
        uninstall.add_argument("--keep-config", action="store_true",
                               help="remove hooks, cron job and routes but keep the loop config file")
        uninstall.add_argument("--admin-token", default="",
                               help="login whose token can delete hooks (admin:repo_hook or repo)")
        uninstall.add_argument("--keep-hooks", action="store_true",
                               help="leave the repo hooks live (explicit opt-out)")
        uninstall.add_argument("--purge", action="store_true",
                               help="also delete the loop's default state directory")
        uninstall.set_defaults(func=cmd_uninstall)

    summary = "Diaktoros — bounded, autonomous software maintenance: review, fix, triage"
    description = ("Configure, inspect and drive Diaktoros loops. Each loop is one JSON file under "
                   "~/.hermes/diaktoros.d/, and it drives two webhook routes, two GitHub hooks "
                   "and (optionally) one cron watchdog job.")

    def deprecated(parser) -> None:  # noqa: ANN001
        """``hermes review-loop`` (#425): the same commands, each saying once that it was renamed.

        It never becomes ``_PARSER``: verbs one command runs for another go through the real name.
        """
        kept = cli_common._PARSER
        setup(parser)
        cli_common._PARSER = kept if kept is not None else parser
        if parser.get_default("func") is not None:          # the bare command prints its usage
            parser.set_defaults(func=_renamed(parser.get_default("func")))
        for action in parser._subparsers._group_actions if parser._subparsers else ():
            for verb in action.choices.values():
                func = verb.get_default("func")
                if func is not None:
                    verb.set_defaults(func=_renamed(func))

    # The old name first and the full name last: a host that keeps one registration keeps that one.
    ctx.register_cli_command("review-loop", "Renamed: use `hermes dk` (Diaktoros)", deprecated,
                             description=description)
    ctx.register_cli_command("dk", summary + " (short for `hermes diaktoros`)", setup,
                             description=description)
    ctx.register_cli_command("diaktoros", summary, setup, description=description)
