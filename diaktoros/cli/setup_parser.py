"""``hermes dk setup``'s argument parser (the command is ``setup_command``; split from it at its
size limit, as ``set_parser`` was from ``set_command``).
"""
from __future__ import annotations

from . import seat_flags
from . import setup_command
from ..config import prices


def add_setup(sub) -> None:  # noqa: ANN001
    """The ``setup`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    first = sub.add_parser("setup", help="First install in one command: runtime paths, init, "
                                         "watchdog, doctor, selftest, then arm when asked "
                                         "(safe to re-run)")
    first.add_argument("--repo", help="owner/name (asked when not given)")
    first.add_argument("--id", help="loop id (default: the repository name)")
    _setup_run_flags(first)
    _setup_identity_flags(first)
    _setup_review_only_flags(first)
    _setup_review_flags(first)
    _setup_switch_flags(first)
    _setup_turn_flags(first)
    first.set_defaults(func=setup_command.cmd_setup)


def _setup_run_flags(first) -> None:  # noqa: ANN001
    """``setup``: how it runs: unattended, dry, protecting the base, arming, and its first answers."""
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
                       ("reviewer-token", "reviewer's token file"),
                       ("fixer-token", "fixer's token file"),
                       ("read-token", "reader login (its own account)"),
                       ("read-token-file", "reader's token file"),
                       ("host", "your gateway's webhook origin"),
                       ("admin-token-file", "hook admin's token file"),
                       ("schedule", "watchdog interval (default 15m)"),
                       ("watchdog-deliver", "where watchdog alerts go (default local)")):
        first.add_argument(f"--{flag}", default="", help=what)


def _setup_identity_flags(first) -> None:  # noqa: ANN001
    """``setup``: App credentials, the hook admin, and the optional routes' profiles."""
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


def _setup_review_only_flags(first) -> None:  # noqa: ANN001
    """``setup``: review-only authors, their limits, and operator agents."""
    first.add_argument("--review-only-cap", default=None, metavar="N",
                       help="verdicts the reviewer gives one review-only PR before it waits "
                            "for `review --another-round`, 1-1000 (default: the plugin "
                            "setting, else the review cap)")
    first.add_argument("--ci-fix-cap", default=None, metavar="N",
                       help="CI-fix turns per PR before a red head waits for you, 1-10 "
                            "(default: the plugin setting, else 3)")
    first.add_argument("--change-proof-max", default=None, metavar="N",
                       help="changed tests the change proof runs per head, 1-50 (default: the plugin setting, else 20)")
    first.add_argument("--reply-settle-seconds", type=int, default=None, metavar="N",
                       help="seconds a fix turn a maintainer's reply woke waits before it starts, "
                            "0-3600, 0 = at once (default: the plugin setting, else 120)")
    first.add_argument("--provider-breaker-failures", default=None, metavar="N",
                       help="turns in a row killed by a provider error before the account waits, "
                            "0-20, 0 = off (default: the plugin setting, else 3)")
    first.add_argument("--review-only-daily", default=None, metavar="N",
                       help="reviewer turns a day on review-only PRs, 1-1000 (default: the "
                            "plugin setting, else no cap)")
    first.add_argument("--review-only", action="append", default=None,
                       help="a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it, or comma-separated) (default: the plugin setting)")
    first.add_argument("--operator-agent", action="append", default=None,
                       help="a GitHub login of an agent that posts as the operator's own account; its comments reach the seats as maintainer data, labelled operator agent (repeat it, or comma-separated) (default: the plugin setting)")


def _setup_review_flags(first) -> None:  # noqa: ANN001
    """``setup``: what gates and informs a review: required checks, the shadow, reserved paths."""
    first.add_argument("--required-check", action="append", default=None,
                       help="a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates) (default: the plugin setting)")
    first.add_argument("--shadow-profile", default=None,
                      help="the Hermes profile shadow reviews run as (#218): a second reviewer model whose verdicts are recorded, never posted (default: the plugin setting, else none)")
    first.add_argument("--shadow-every", type=int, default=None,
                      help="shadow one in this many reviewer turns, 1-1000 (default: the plugin setting, else 5)")
    first.add_argument("--shadow-escalate", choices=("on", "off"), default=None,
                       help="on: a PR is not ready to merge while its shadow review is running, or after it requested changes (#1004); needs --shadow-every 1 or a --shadow-always-path (default: the plugin setting, else off)")
    first.add_argument("--shadow-file-issues", choices=("on", "off"), default=None,
                       help="on: the issues a shadow review asks to file are filed by the reader once the real review exists and did not file them, labelled shadow-found, up to --shadow-issues-daily a day (#914) (default: the plugin setting, else off)")
    first.add_argument("--shadow-issues-daily", default=None, metavar="N",
                       help="most shadow-found issues filed per rolling 24 hours, 0-1000; 0 = no cap (#1515) (default: the plugin setting, else 25)")
    first.add_argument("--shadow-always-path", action="append", default=None, metavar="GLOB",
                       help="a glob for paths whose change always gets a shadow review, whatever --shadow-every says (#1788; repeat it; '*' also matches '/') (default: the plugin setting)")
    first.add_argument("--shadow-never-path", action="append", default=None, metavar="GLOB",
                       help="a glob for paths a PR may change without a shadow review: one that changes only these gets none (#1788; repeat it; an always path wins) (default: the plugin setting)")
    first.add_argument("--human-path", action="append", default=None,
                       help="a glob pattern for paths only a human may approve: the loop's approval of a diff touching one is left to a person (repeat it; none = no path reserved) (default: the plugin setting)")
    first.add_argument("--generated", action="append", default=None, metavar="PATH[,PATH]=COMMAND",
                       help="generated files the host regenerates when a PR's only merge conflicts are in them: PATH[,PATH...]=COMMAND, run in the sandbox on the merged tree (repeat it; none = off) (default: the plugin setting)")
    first.add_argument("--ci-covers", action="append", default=None, metavar="CHECK=COVERS",
                       help="what a CI check covers, told to the reviewer (#756): CHECK=COVERS, e.g. 'tests (3.14)=full unit suite and harness' (repeat it; none = the reviewer is told nothing) (default: the plugin setting)")
    first.add_argument("--price", action="append", default=None, metavar="MODEL=IN/CACHED/OUT",
                       help=prices.HELP + " (default: the plugin setting)")


def _setup_switch_flags(first) -> None:  # noqa: ANN001
    """``setup``: where events arrive, the merge check, the always-run check, and the on/off switches."""
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
    for flag, text, default in (
            ("--review-after-ci", "start each review after the head's checks finish (up to an "
                                  "hour)", "off"),
            ("--fix-ci", "hand a failed required check on a fixer's PR to the fixer", "off"),
            ("--ci-webhook", "act on a finished CI run when GitHub reports it: the fixer's hook "
                             "also carries check_suite", "off"),
            ("--comment-webhook", "wake the issue fixer on a maintainer's issue reply when GitHub "
                                  "reports it: the triage hook also carries issue_comment", "off"),
            ("--protection-webhook", "re-check the base's protection when its rules change: the "
                                     "reviewer's hook also carries branch_protection_rule and "
                                     "repository_ruleset", "off"),
            ("--fixer-scope-lock", "hold a fix round that changes files its findings do not name "
                                   "for a maintainer", "off"),
            ("--adjudicator-approves", "on an ACCEPT ruling with green checks, approve as the "
                                       "adjudicator login and dismiss the change request it "
                                       "overruled", "off"),
            ("--adjudicator-file-issues", "file the findings a ruling lets through as issues, by the reader, labelled adjudicator-found", "off"),
            ("--change-proof", "run each reviewed head's changed tests on its merge base and at the head, and show the reviewer", "off"),
            ("--main-flake-rerun", "rerun main's failed CI jobs once when they look like a flake, before a red-main notice", "off"),
            ("--update-behind", "merge the base into a loop PR that fell behind, keeping its approval when the merge touches nothing the PR changed", "off"),
            ("--review-only-rerequest-grants", "a maintainer's GitHub re-request at a review-only PR's cap grants one more verdict", "off"),
            ("--attribution", "sign what the loop posts with 'Automated by Diaktoros'", "on")):
        first.add_argument(flag, choices=("on", "off"), default=None,
                           help=f"{text} (default: the plugin setting, {default})")


def _setup_turn_flags(first) -> None:  # noqa: ANN001
    """``setup``: the per-seat table shared with ``init`` and ``set`` (#1484): profiles, steps,
    daily tokens, and the shadow and triage capacity; and the runtime paths."""
    seat_flags.add(first, "setup")
    first.add_argument("--fix-daily-turns", type=int, default=None,
                       help="issue-fix turns per day: refused here, a new loop has no "
                            "triage block; set it with `triage --enable ... --fix-daily-turns N` "
                            "(0 = ignore)")
    for key in ("source", "venv", "runtime", "rust"):
        first.add_argument(f"--{key}", default="",
                           help=f"runtime file's {key} path (default: detected)")
    first.add_argument("--node", default=None,
                       help="runtime file's node: the Node install root seats get at /opt/node, "
                            "\"\" for none (default: detected, else kept)")
