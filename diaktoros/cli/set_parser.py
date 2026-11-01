"""``hermes dk set``'s argument parser (#1349, split from ``settings``; the command is
``set_command``).
"""
from __future__ import annotations

from . import set_command


def add_set(sub) -> None:  # noqa: ANN001
    """The ``set`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    change = sub.add_parser("set", help="Change a loop's settings in place")
    change.add_argument("--loop", required=True,
                        help="loop id (its config file name; `list` shows them)")
    change.add_argument("--concurrency", type=int,
                        help="default PRs per seat at once (1 = serialized; above 1 needs a "
                             "clone unless the isolated worker's runtime file exists, since each run gets its own)")
    change.add_argument("--reviewer-concurrency", type=int, default=None,
                        help="how many PRs the reviewer may work at once")
    change.add_argument("--fixer-concurrency", type=int, default=None,
                        help="how many PRs the fixer may work at once")
    change.add_argument("--shadow-concurrency", type=int, default=None,
                        help="how many shadow reviews may run at once (>= 1; only the shadow "
                             "seat's own value: the loop default does not reach it)")
    change.add_argument("--triage-concurrency", type=int, default=None,
                        help="how many issue triages may run at once (>= 1; only the triage "
                             "seat's own value: the loop default does not reach it)")
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
    change.add_argument("--fixer-scope-lock", choices=("on", "off"), default=None,
                        help="hold a fix round that changes files its findings do not name "
                             "(other than their tests and docs) for a maintainer, or stop; "
                             "needs unattended fixer pushes")
    change.add_argument("--review-only-cap", default=None, metavar="N",
                        help="verdicts the reviewer gives one review-only PR before it waits "
                             "for `review --another-round`, 1-1000; 0 = the review cap")
    change.add_argument("--ci-fix-cap", default=None, metavar="N",
                        help="CI-fix turns per PR before a red head waits for you, "
                             "1-10; 0 = the default (3)")
    change.add_argument("--review-only-daily", default=None, metavar="N",
                        help="reviewer turns a day on review-only PRs, 1-1000; 0 = no cap")
    reviewed = change.add_mutually_exclusive_group()
    reviewed.add_argument("--review-only", action="append", default=None,
                          help="a GitHub login whose PRs the reviewer reviews but the fixer never touches (repeat it, or comma-separated); replaces the list")
    reviewed.add_argument("--no-review-only", action="store_true",
                          help="clear the review-only list")
    agents = change.add_mutually_exclusive_group()
    agents.add_argument("--operator-agent", action="append", default=None,
                        help="a GitHub login of an agent that posts as the operator's own account; its comments reach the seats as maintainer data, labelled operator agent (repeat it, or comma-separated); replaces the list")
    agents.add_argument("--no-operator-agents", action="store_true",
                        help="clear the operator-agents list")
    required = change.add_mutually_exclusive_group()
    required.add_argument("--required-check", action="append", default=None,
                          help="a check run or status context that gates an approval, exactly as GitHub names it (repeat it; none = every check gates); replaces the list")
    required.add_argument("--no-required-checks", action="store_true",
                          help="clear the list: every check gates again")
    shadows = change.add_mutually_exclusive_group()
    shadows.add_argument("--shadow-profile", default=None,
                         help="the Hermes profile shadow reviews run as (#218): a second reviewer "
                              "model whose verdicts are recorded, never posted; 'off' turns them off")
    shadows.add_argument("--no-shadow", action="store_true",
                         help="turn shadow reviews off")
    change.add_argument("--shadow-every", type=int, default=None,
                        help="shadow one in this many reviewer turns, 1-1000 (needs a shadow profile)")
    change.add_argument("--shadow-escalate", choices=("on", "off"), default=None,
                        help="on: a PR is not ready to merge while its shadow review is running, or after it requested changes (#1004); needs --shadow-every 1")
    change.add_argument("--shadow-file-issues", choices=("on", "off"), default=None,
                        help="on: the issues a shadow review asks to file are filed by the reader once the real review exists and did not file them, labelled shadow-found, up to --shadow-issues-daily a day (#914); needs a shadow profile")
    change.add_argument("--shadow-issues-daily", default=None, metavar="N",
                        help="most shadow-found issues filed per rolling 24 hours, 0-1000; 0 = no cap (#1515, default 25); needs a shadow profile")
    humans = change.add_mutually_exclusive_group()
    humans.add_argument("--human-path", action="append", default=None,
                        help="a glob pattern for paths only a human may approve (repeat it); replaces the list")
    humans.add_argument("--no-human-paths", action="store_true",
                        help="clear the list: no path is reserved for a human")
    covers = change.add_mutually_exclusive_group()
    covers.add_argument("--ci-covers", action="append", default=None, metavar="CHECK=COVERS",
                        help="what a CI check covers, told to the reviewer (#756): CHECK=COVERS, e.g. 'tests (3.14)=full unit suite and harness' (repeat it); replaces the map")
    covers.add_argument("--no-ci-coverage", action="store_true",
                        help="clear the map: the reviewer is told nothing about what a check covers")
    generated = change.add_mutually_exclusive_group()
    generated.add_argument("--generated", action="append", default=None, metavar="PATH[,PATH]=COMMAND",
                           help="generated files the host regenerates when a PR's only merge conflicts are in them: PATH[,PATH...]=COMMAND, run in the sandbox (repeat it); replaces the list")
    generated.add_argument("--no-generated", action="store_true",
                           help="clear the list: the conflict shortcut is off")
    change.add_argument("--ingress", choices=("repo-hooks", "app-webhook"), default=None,
                        help="where events arrive: repo-hooks (the default: per-loop repo hooks) or app-webhook (a GitHub App's own webhook, --ingress-app; the loop then needs no repo hooks)")
    change.add_argument("--ingress-app", default=None, help="with --ingress app-webhook: the bot login of the GitHub App whose webhook delivers (mapped with --app)")
    change.add_argument("--merge-check", choices=("auto", "on", "off"), default=None,
                        help="post the diaktoros/review merge check: auto (on when the reviewer is a GitHub App), on (a PAT loop gets a commit status) or off")
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
    change.add_argument("--shadow-daily-tokens", type=int, default=None,
                        help="most tokens shadow reviews may spend per day on this loop; once "
                             "spent, the next waits for midnight (0 removes it)")
    change.add_argument("--shadow-daily-turns", type=int, default=None,
                        help="most shadow reviews per day on this loop (0 removes the cap)")
    change.add_argument("--shadow-turn-budget", type=int, default=None,
                        help="a shadow review's own turn budget in seconds (0 = the loop's)")
    change.add_argument("--shadow-max-steps", type=int, default=None,
                        help="agent steps one shadow review may take, 8-200 (0 = default 100)")
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
    change.add_argument("--app", action="append", default=[], metavar="LOGIN=APP_ID:KEY",
                        help="<app-slug>[bot]=APP_ID:/abs/path/to/key.pem: a GitHub App identity "
                             "instead of a PAT for the --read-token or --adjudicator-login login, "
                             "or an extra login such as a hook admin (--admin-token); the key "
                             "stays on the host, mode 0600")
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
    change.set_defaults(func=set_command.cmd_set)
