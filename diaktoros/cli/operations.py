"""Day-to-day operating commands (#676): ``status``, ``list``, ``retry``, ``explain``, ``ready``,
``review``, ``escalate``, ``regression``, ``depends``, ``drain`` and ``cleanup``.

What the loop is doing and the operator's direct steps on one PR or issue. ``cli`` re-exports
every command here, so ``cli.cmd_status`` and the parser's dispatch are unchanged.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import time

from .. import (
    attribution,
    config,
    gate,
    gh,
    observer,
    pr_hold,
    routes,
    scope_lock,
    state as state_mod,
)
from ..config import credentials
from .common import _budget_line, _credential_lines, _routes_of


def _route_state(loop: dict, role: str) -> str:
    """What the *installed* route serves, next to what the loop says it should serve.

    The mismatch is the interesting case: a seat whose profile moved but whose route did not is a
    loop that runs as the old agent while every config file claims otherwise.
    """
    name = _routes_of(loop).get(role) or ""
    if not name:
        return f"{role}: (no route)"
    want = config.seat_profile(loop, role)
    entry = routes.route(name)
    if not entry:
        if role in ("observer", "observer_urgent"):
            # init refuses an existing loop, so "run init" would be a dead end for the feed.
            return f"{role} {name}: not installed — {observer.route_remedy(loop).strip('`')}"
        return f"{role} {name}: not installed — run init"
    got = routes.route_profile(entry)
    if got is None:
        return (f"{role} {name} → {entry.get('profile')!r} (blank — the gateway refuses it), "
                f"not {want}: MISMATCH — hermes dk apply --loop {loop['id']}")
    if got == want:
        muted = role in ("observer", "observer_urgent") and (loop.get("observer") or {}).get("mute")
        return f"{role} {name} → {got} (ok{', muted' if muted else ''})"
    return (f"{role} {name} → {got}, not {want}: MISMATCH — "
            f"hermes dk apply --loop {loop['id']}")


def _pinned_seat_notes(loop: dict) -> list[str]:
    """Seats held at 1 by their own value while the loop default asks for more.

    ``init`` before #76 wrote ``seats.<seat>.concurrency: 1`` into every loop whether or not it
    was asked for, so this is the shape such a loop has after someone raised ``concurrency``. It
    is also exactly what an explicit ``--fixer-concurrency 1`` looks like, and the two cannot be
    told apart from the file — so the value is kept, and the `set` that raises it is named.
    """
    notes = []
    for seat in ("reviewer", "fixer"):
        own = (loop["seats"].get(seat) or {}).get("concurrency")
        if own == 1 and loop.get("concurrency", 1) > 1:
            notes.append(f"{seat} is pinned at 1 by seats.{seat}.concurrency (loop default "
                         f"{loop['concurrency']}) — to raise it: `hermes dk set "
                         f"--loop {loop['id']} --{seat}-concurrency {loop['concurrency']}`")
    return notes


def _steps_line(loop: dict) -> str:
    """Each seat's agent-step cap per turn (#271); an issue fix takes the fixer's."""
    seats = ["reviewer", "fixer"]
    if (loop.get("adjudicator") or {}).get("route"):
        seats.append("adjudicator")
    if (loop.get("triage") or {}).get("route"):
        seats.append("triage")
    return " · ".join(f"{seat} {config.max_steps(loop, seat)}" for seat in seats)


def _readable_loops() -> tuple[list[dict], list[str]]:
    """Every loop that loads, plus one ``skipping <file>: <reason>`` line per one that does not.

    The formatted form of ``config.readable_loops`` for ``list`` and ``status``: one broken file
    must not hide every healthy loop's state. (``explain`` calls ``config.readable_loops``
    itself — it needs the ids, not these lines.) Verbs that act on loops keep ``all_loops``'s
    all-or-nothing refusal.
    """
    loops, skipped = config.readable_loops()
    return loops, [f"skipping {loop_id}.json: {reason}" for loop_id, reason in skipped]


def cmd_list(args) -> int:
    loops, skipped = _readable_loops()
    for line in skipped:
        print(line)
    if not loops:
        if not skipped:
            print(f"no loops configured in {config.config_dir()}")
        return 2 if skipped else 0
    for loop in loops:
        seats = " ".join(f"{seat}={config.seat_concurrency(loop, seat)}"
                         for seat in ("reviewer", "fixer"))
        print(f"{loop['id']:<20} {loop['repo']:<30} cap={loop['cap']} {seats} "
              f"fixers={','.join(loop['fixers'])} reviewers={','.join(loop['reviewers'])}")
    return 2 if skipped else 0


def _dependency_lines(loop: dict, pr: int | None = None, limit: int = 5) -> list[str]:
    """What the host dependency prefetch did for this loop's newest turns (#51), from the ledger.

    Read-only; an absent or unreadable ledger is simply no lines (status/explain say the rest).
    """
    from ..run_supervisor import dependency_view, describe_dependencies
    rows = dependency_view(config.host_path("ledger"), loop["repo"], pr,
                           limit)
    return [describe_dependencies(row) for row in rows or []]


def _view_lines(loop: dict, pr: int, head: str | None, limit: int = 3) -> list[str]:
    """This PR's turns whose seat could not see the whole change (#93, #110), from the ledger.

    Only the current head's: a new head gets a new change record. Read-only; an absent or
    unreadable ledger is no lines.
    """
    from ..run_supervisor import describe_view, view_view
    rows = view_view(_ledger_path(), loop["repo"], pr, limit) or []
    return [describe_view(row) for row in rows if head and row["head"] == head]


def cmd_status(args) -> int:
    skipped: list[str] = []
    if args.loop:
        try:
            loops = [config.load_id(args.loop)]
        except config.ConfigError as exc:
            print(f"cannot show loop: {exc}")
            return 2
    else:
        loops, skipped = _readable_loops()
        for line in skipped:
            print(line)
        if not loops and not skipped:
            print(f"no loops configured in {config.config_dir()} — run "
                  "`hermes dk setup` to create one")
    for loop in loops:
        from .. import state as state_mod

        st = state_mod.state_for(loop)
        print()
        print(f"[{loop['id']}] {loop['repo']}  (cap {loop['cap']}, base {loop['base']})")
        print("  parallel:   " + " · ".join(
            f"{seat} {config.seat_concurrency(loop, seat)}"
            + ("" if config.seat_concurrency(loop, seat) > 1 else " (serialized)")
            for seat in ("reviewer", "fixer")))
        for line in _pinned_seat_notes(loop):
            print(f"  note:       {line}")
        print(f"  clone:      {loop['clone'] or '(none)'}")
        print(f"  turn:       {_budget_line(loop)} per turn (killed past it)")
        print(f"  steps:      {_steps_line(loop)} per turn (agent steps; `set --reviewer-max-steps "
              "N`/`--fixer-max-steps N`)")
        for line in _pacing_lines(loop):
            print(f"  pacing:     {line}")
        print("  fixer push: " + ("ENABLED — operator accepted PR-metadata/ref race"
                                  if config.unattended_fixer_push_enabled(loop)
                                  else "off (unattended pushes disabled)"))
        print("  signed:     " + ("on — what the loop posts says 'Automated by Diaktoros'"
                                  if attribution.enabled(loop)
                                  else "off (no footer or commit trailer)"))
        print(f"  state:      {st.dir}")
        print(f"  seats:      reviewer={loop['seats']['reviewer']['login']} "
              f"({loop['seats']['reviewer']['profile']}) · "
              f"fixer={loop['seats']['fixer']['login']} ({loop['seats']['fixer']['profile']})")
        adjudicator = loop.get("adjudicator") or {}
        if adjudicator.get("route"):
            adj_login = config.adjudicator_login(loop)
            print(f"  {'adjudicator:':<12} {adjudicator.get('profile', 'default')} "
                  f"(route {adjudicator['route']})"
                  + (f" · comments as {adj_login}" if adj_login else " · operator-only rulings"))
        else:
            print(f"  {'adjudicator:':<12} (no route — the cap only writes a marker)")
        # What the registry actually serves, next to what the config claims: those two facts can
        # disagree after a profile change, and this is the one place the operator would see it.
        print("  routes:     " + " · ".join(_route_state(loop, role)
                                            for role in (*config.ROUTED_ROLES, "observer",
                                                         "observer_urgent", "app")
                                            if role in _routes_of(loop)))
        refs = _credential_lines(loop)
        if refs:
            print("  credentials: " + " · ".join(refs))   # a PAT file or a GitHub App (#1163)
        problem = credentials.reader_problem(loop)
        if problem:
            print(f"  ⚠️  reader:  {problem} — {config.FOUR_IDENTITY_RULE}")
            print(f"  fix:        {credentials.reader_fix(loop)}")
        for seat, entries in st._lock_ledger().items():
            for key, entry in (entries if isinstance(entries, dict) else {}).items():
                at = state_mod.mark_at(entry)
                if at is None:
                    # Shown, not hidden: the file holds it, but no reader counts it as running
                    # and the next write of the ledger drops it (#80).
                    print(f"  ⚠️  lock:    {seat} mark for {key} is unreadable (no numeric 'at') — "
                          "not counted as running; the next sweep prunes it")
                    continue
                held = (time.time() - at) / 60
                if held < 0:
                    # Not live (#794): no reader counts it, and the next sweep prunes it.
                    print(f"  ⚠️  lock:    {seat} mark for {key} is dated in the future — "
                          "not counted as running; the next sweep prunes it")
                    continue
                print(f"  running:    {seat} on {key} for {held:.0f}m")
        for line in _dependency_lines(loop):
            print(f"  deps:       {line}")
        for issue_no, origin in sorted(st.fix_holds().items()):
            print(f"  held:       issue #{issue_no} fix waits for PR #{origin} to merge")
        from .. import fix_hold
        for issue_no, ask in sorted(st.fix_ask_open().items()):
            print(f"  held:       {fix_hold.ask_line(issue_no, ask)}")
        declared = st.issue_deps()
        for issue_no in st.dep_waits():
            names = ", ".join(f"#{a}" for a in sorted(declared.get(issue_no) or {})) or "?"
            print(f"  held:       issue #{issue_no} fix waits for prerequisite {names} "
                  f"(`hermes dk depends --loop {loop['id']} --issue {issue_no}` says why)")
        for issue_no, late in sorted(st.late_triage().items()):
            print(f"  late:       issue #{issue_no} triage queued late "
                  f"({time.strftime('%Y-%m-%d %H:%M', time.gmtime(late['at']))} UTC): the "
                  "webhook delivery was missed (see `doctor`: gate:rate_limit)")
        for pr_no, hold in sorted(st.pr_holds().items()):
            print(f"  held:       PR #{pr_no} {pr_hold.line(hold, loop)}")
        for pr_no, hold in sorted(st.scope_holds().items()):
            print(f"  held:       {scope_lock.line(pr_no, hold)}")
        for seat in ("reviewer", "fixer"):
            queued = len(st.queue_items(seat))
            if queued:
                print(f"  queued:     {seat} {queued} "
                      f"({config.seat_concurrency(loop, seat)} at a time)")
        queue = st.queue_all()
        for seat, items in (queue if isinstance(queue, dict) else {}).items():
            for key, entry in (items if isinstance(items, dict) else {}).items():
                if state_mod.mark_at(entry) is None:
                    print(f"  ⚠️  queued:  {seat} · {key} is unreadable (no numeric 'at') — "
                          "the next drain drops it")
                    continue
                print(f"  queued:     {seat} · {key} — {entry.get('reason')}")
        breaches = st.breach_all()
        if breaches:
            for key, entry in breaches.items():
                print(f"  breach:     {key} at {(entry.get('head') or '')[:7]} "
                      f"— {entry.get('status')}")
        observer_cfg = loop.get("observer") or {}
        print(f"  observer:   {observer.describe(observer_cfg)}")
        if not observer_cfg and loop.get("observer_disabled"):
            print("  observer:   disabled — existing notices remain owed; restoring the original "
                  "destination can resume them")
        if observer_cfg or loop.get("observer_disabled"):
            counts = observer.owed(st)
            owed = sum(n for status, n in counts.items() if status != "delivered")
            print(f"  observer:   {counts.get('delivered', 0)} delivered · {owed} owed "
                  f"(failed or waiting)")
            _print_uncertain(st, loop["id"])
            # Only a live feed can be broken: a muted or absent one is reported above, and calling
            # that a problem would be crying wolf at a setting the operator chose.
            if observer_cfg and not observer.configured(loop):
                problem = observer.unusable(loop)
                if problem:
                    print(f"  observer:   ⚠ {problem}")
        watch = st.watch()
        if watch.get("last_run"):
            print(f"  watchdog:   last run {watch['last_run']}")
        _print_ledger_runs(loop, None, "  runs:       ", limit=10)
    return 2 if skipped else 0


def _print_uncertain(st, loop_id: str) -> None:
    """Each settleable (uncertain) notice: key, event, number, age, error, and how to settle."""
    rows = observer.owed_entries(st)
    now = time.time()
    for key, entry in rows:
        age = now - (state_mod.mark_at(entry) or now)
        print(f"    uncertain: {key} · {entry.get('event')} #{entry.get('number')} · "
              f"{int(age // 86400)}d {int(age % 86400 // 3600)}h old · {entry.get('error')}")
    if rows:
        print(f"    after checking the chat: hermes dk observer --loop {loop_id} --settle KEY "
              "(--delivered | --not-delivered)")


def cmd_observer(args) -> int:
    """List or settle the observer's uncertain notices (#724)."""
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    st = state_mod.state_for(loop)
    if args.settle:
        if args.delivered == args.not_delivered:
            print("refused: --settle needs exactly one of --delivered or --not-delivered")
            return 2
        try:
            print(observer.settle(st, args.settle, delivered=args.delivered))
        except observer.SettleError as exc:
            print(f"refused: {exc}")
            return 2
        return 0
    rows = observer.owed_entries(st)
    if not rows:
        print("no uncertain observer notices")
    for key, entry in rows:
        print(f"{key} · {entry.get('event')} #{entry.get('number')} · {entry.get('error')}")
    return 0


def add_observer(sub) -> None:  # noqa: ANN001
    """The ``observer`` subcommand's parser, registered by ``cli.register_cli`` (#724)."""
    obs = sub.add_parser("observer", help="List or settle uncertain observer notices")
    obs.add_argument("--loop", required=True,
                     help="loop id (its config file name; `list` shows them)")
    obs.add_argument("--list", action="store_true", help="list the uncertain notices (default)")
    obs.add_argument("--settle", metavar="KEY",
                     help="settle one uncertain notice after checking the chat by hand")
    obs.add_argument("--delivered", action="store_true",
                     help="with --settle: it arrived; record a receipt, send nothing")
    obs.add_argument("--not-delivered", dest="not_delivered", action="store_true",
                     help="with --settle: it did not arrive; the next sweep retries it once "
                          "with its original delivery id")
    obs.set_defaults(func=cmd_observer)


def _ledger_path() -> pathlib.Path:
    return config.host_path("ledger")


def _print_ledger_runs(loop: dict, pr: int | None, prefix: str, limit: int) -> list[dict]:
    """The isolated run ledger's failed/waiting/uncertain rows for this loop, read-only (#53).
    Prints them and returns them ([] when there is no readable ledger)."""
    from ..run_supervisor import describe_run, pending_view, read_only_view, split_history

    ledger = _ledger_path()
    if not ledger.exists():
        return []
    queue = pending_view(ledger, loop["repo"]) or []
    for place, row in enumerate(queue, 1):
        if pr is None or row["pr"] == pr:
            print(f"{prefix}queue {place} of {len(queue)}: {row['seat']} #{row['pr']} "
                  f"@ {str(row['head'])[:7]} — {row['why']}")
    rows = read_only_view(ledger, loop["repo"], pr)
    if rows is None:
        print(f"{prefix}run ledger unreadable ({ledger})")
        return []
    # A failed run on a PR the watchdog's last complete listing does not show open is history
    # (#723): counted apart, never offered for re-arm. The ledger rows are not touched.
    rows, history, unknown = split_history(rows, loop)
    if history:
        if pr is None:
            print(f"{prefix}{len(history)} failed turn(s) on closed PRs, history "
                  f"(#{', #'.join(str(n) for n in sorted({r['pr'] for r in history}))}): nothing "
                  "to re-arm")
        else:
            for row in history:
                print(f"{prefix}{row['seat']} #{row['pr']} @ {str(row['head'])[:7]} failed — "
                      f"{row['error'] or 'no reason recorded'}; PR #{pr} is not open (merged or "
                      "closed): history, nothing to re-arm")
    if unknown and any(r["state"] == "failed" for r in rows):
        print(f"{prefix}note: no open-PR listing from the watchdog yet — whether these PRs are "
              "still open is unknown")
    for row in rows[-limit:]:
        print(prefix + describe_run(row, loop["id"]))
        if row.get("detail") and pr is not None:
            for line in str(row["detail"]).splitlines()[-6:]:
                print(f"{' ' * len(prefix)}| {line[:200]}")
    if len(rows) > limit:
        print(f"{prefix}… {len(rows) - limit} more: python -m diaktoros.run_supervisor "
              f"status {ledger}")
    return rows


def _print_shadow_note(report: dict, prefix: str) -> None:
    """``explain``'s note when a held loop has no shadow review to wait for at this head (#1004):
    none was queued, or its run ended without one."""
    note = (report.get("shadow") or {}).get("note")
    if note:
        print(f"{prefix}{note}")


def _print_shadows(loop: dict, pr: int, prefix: str) -> None:
    """This PR's shadow reviews (#218), each beside the real reviewer's verdict at its head.
    Data for comparing models: nothing in the loop acts on them."""
    from .. import stats
    shown, older = stats.shadow_reviews_for(_ledger_path(), loop["repo"], pr)
    for item in shown:
        real = ("no real verdict yet" if item["real"] is None else
                f"reviewer {item['real']} ({'agrees' if item['agrees'] else 'disagrees'})")
        held = f", left to a person: {item['held']}" if item["held"] else ""
        print(f"{prefix}{item['model']} at {item['head']} would have {item['verdict']}{held}"
              f" · {real} · recorded, never posted")
        for finding in item["findings"]:
            print(f"{' ' * len(prefix)}| {finding}")
        if item["more"]:
            print(f"{' ' * len(prefix)}| … {item['more']} more")
    if older:
        print(f"{prefix}… {older} older shadow review(s) not shown")


def cmd_retry(args) -> int:
    """Re-arm a PR's isolated run that failed before any external write (issue #53).

    Only runs at the PR's newest ledgered head are considered: the head of the most recently
    active run (``updated``), which is where the PR is after a backwards force-push re-armed an
    older head's run. A run that may have written —
    uncertain, quarantined, reconciled, or with a receipt claim, push intent or ruling on
    record — is refused with the reconcile instructions; it is never replayed.

    The selection and the re-arm are ``retry_action``'s, which the Desktop page's Retry button
    calls too (#371): this function only supplies the CLI's two GitHub reads and prints the
    result. Its PR read is the supersession tiebreaker (#126) and the #723 open check; the
    Desktop, which has neither, uses the ledger-only head those answers fall back to.
    """
    from .. import gh, retry_action

    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2

    def read_pr():
        return gh.pr(loop, args.pr)

    def resolve_head():
        pr = read_pr()
        return ((pr or {}).get("head") or {}).get("sha") if isinstance(pr, dict) else None

    result = retry_action.perform(loop, args.pr, args.seat, ledger=_ledger_path(),
                                  resolve_head=resolve_head, read_pr=read_pr, reveal_errors=True)
    released = result["released"]
    if released:
        print(f"[{loop['id']}] #{args.pr}: scope lock released for "
              f"{', '.join(released.get('files') or [])}; the fixer's next push of that head "
              "goes through")
    if result["message"]:
        print(result["message"])
        return 2
    for entry in result["rearmed"]:
        label = (f"{entry['seat']} #{args.pr} @ {entry['head7']}"
                 + (f" ({entry['turn_key']})" if entry.get("turn_key") else ""))
        print(f"[{loop['id']}] {label} re-armed (was {entry['state']}: "
              f"{entry.get('error') or 'no reason'})")
    for entry in result["refused"]:
        label = (f"{entry['seat']} #{args.pr} @ {entry['head7']}"
                 + (f" ({entry['turn_key']})" if entry.get("turn_key") else ""))
        print(f"[{loop['id']}] {label} {entry['state']}: {entry['reason']}")
    if result["rearmed"] and result["worker"]:
        if result["worker"] == "started":
            print("worker started")
        else:
            print(result["worker"] + (" — the next event or armed watchdog sweep starts it"
                                      if result["worker"].startswith("worker not started")
                                      else ""))
    return 0 if result["ok"] else 2


def cmd_continue(args) -> int:
    """Finish a half-written issue fix (#485): inspect, then run only the missing stage.

    Never pushes. Conflicting facts leave the run quarantined and are printed. Exit 0 when
    the fix is complete, 2 otherwise.
    """
    from .. import continue_run
    from ..run_supervisor import Supervisor

    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    ledger = _ledger_path()
    if not ledger.exists():
        print(f"no run ledger at {ledger} — nothing to continue")
        return 2
    sup = Supervisor(ledger)
    if args.dry_run:
        plan = continue_run.inspect(loop, sup, args.run)
        print("\n".join(plan["facts"] + [continue_run.offer(plan, loop["id"], args.run)]))
        return 0 if not plan["conflicts"] else 2
    ok, lines = continue_run.run(loop, sup, args.run)
    print("\n".join(lines))
    return 0 if ok else 2


def _pacing_lines(loop: dict) -> list[str]:
    """Daily caps and today's counts per seat, and any seat account held for its reset (#219).

    The data is ``pacing.seats`` — the same view the Desktop page reads — so a status line and
    the page cannot disagree about whether a window is closed.
    """
    from .. import pacing
    data = pacing.seats(loop)
    seats = [(seat, data["seats"][seat]) for seat in ("reviewer", "fixer")]

    def tokens_text(entry: dict) -> str:
        cap = f"{entry['tokens_cap']:,}" if entry["tokens_cap"] else "no budget"
        return f"{entry['tokens']:,}/{cap} tokens today"

    lines = []
    if any(entry["turns_cap"] for _, entry in seats):
        lines.append(" · ".join(f"{seat} {entry['turns']}/{entry['turns_cap'] or 'no cap'} today"
                                for seat, entry in seats))
    if any(entry["tokens_cap"] for _, entry in seats):
        lines.append(" · ".join(f"{seat} " + tokens_text(entry) for seat, entry in seats))
    for hold in data["holds"]:
        lines.append(f"held: {hold['provider']} as profile {hold['profile']} until "
                     f"{pacing.when(hold['until'])} ({hold['reason']})")
    return lines


def _reviewer_runs(repo: str, number: int, head: str) -> int:
    """Compatibility accessor; the shared action owns the ledger read."""
    from ..one_shot_action import _reviewer_runs as count
    return count(repo, number, head)


def cmd_escalate(args) -> int:
    """Thin I/O wrapper for the shared host-owned escalate operation."""
    from .. import one_shot_action
    loop = _one_loop(args)
    if loop is None:
        return 2
    result = one_shot_action.perform_escalate(loop, args.pr, getattr(args, "reason", ""),
                                              withdraw=getattr(args, "withdraw", False))
    for line in result["lines"]:
        print(line)
    return result["code"]


def cmd_depends(args) -> int:
    """Declare, waive or list issue dependencies (#671): issue B's fix waits for issue A's
    implementation to be merged into the base.

    Operator-only: the loop never infers a dependency from issue text. Declaring checks that both
    are issues of this repository, that B is not A, and that it closes no cycle; it holds B's
    next hand-off; a turn already queued is held at claim, launch and the broker's publish check
    until the prerequisite is merged or waived (#775; an unreadable one holds too), but a turn
    already running is not recalled. ``--waive`` removes one,
    and the next watchdog sweep releases B if nothing else holds it. With neither, lists every
    dependency and whether it is met. Exit 1 when refused, 2 when it cannot be answered.
    """
    from .. import issue_deps
    from ..util import now_iso
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
        names = [lp["id"] for lp in loops] + [loop_id for loop_id, _ in refused]
        if len(names) != 1 or refused:
            print(f"{len(names)} loops are configured ({', '.join(names) or 'none'}) — name one "
                  "with --loop")
            return 2
        loop = loops[0]
    st = state_mod.state_for(loop)
    deps = st.issue_deps()
    if args.on is None and args.waive is None:
        if args.issue is not None:
            deps = {args.issue: deps.get(args.issue) or {}}
        if not any(deps.values()):
            print(f"[{loop['id']}] no issue dependencies declared")
            return 0
        for issue, prerequisites in sorted(deps.items()):
            for prerequisite in sorted(prerequisites):
                done, why = issue_deps.met(loop, prerequisite)
                mark = {True: "met", False: "waiting", None: "unknown"}[done]
                print(f"[{loop['id']}] #{issue} depends on #{prerequisite}: {mark} — {why}")
        return 0
    if args.issue is None:
        print("--on and --waive need --issue N (the issue that waits)")
        return 2
    if args.waive is not None:
        if st.issue_dep_drop(args.issue, args.waive):
            print(f"[{loop['id']}] #{args.issue} no longer depends on #{args.waive}; the next "
                  "watchdog sweep releases it if nothing else holds it")
            return 0
        print(f"[{loop['id']}] refused: #{args.issue} does not depend on #{args.waive}")
        return 1
    reason = issue_deps.problem(deps, args.issue, args.on)
    if reason:
        print(f"[{loop['id']}] refused: {reason}")
        return 1
    for number in (args.issue, args.on):
        found = issue_deps.read_issue(loop, number)
        if found is None:
            print(f"[{loop['id']}] #{number} could not be read from GitHub — nothing declared")
            return 2
        if "pull_request" in found:
            print(f"[{loop['id']}] refused: #{number} is a pull request; a dependency joins two "
                  "issues of this repository")
            return 1
    added = st.issue_dep_add(args.issue, args.on, {"by": "operator", "at": now_iso()})
    print(f"[{loop['id']}] #{args.issue} " + ("now depends on" if added else "already depends on")
          + f" #{args.on}: its fix waits until #{args.on}'s implementation is merged into "
            f"{loop['base']}; a turn already queued is held at launch and publish until then "
            "(or until `--waive`), one already running is not recalled")
    return 0


def _one_loop(args) -> dict | None:
    """``--loop``, else the only configured loop; None (said) when that cannot be decided."""
    if args.loop:
        try:
            return config.load_id(args.loop)
        except config.ConfigError as exc:
            print(f"no such loop: {exc}")
            return None
    loops, refused = config.readable_loops()
    for loop_id, reason in refused:
        print(f"skipping {loop_id}.json: {reason}")
    names = [lp["id"] for lp in loops] + [loop_id for loop_id, _ in refused]
    if len(names) > 1:
        print(f"{len(names)} loops are configured ({', '.join(names)}) — name one with --loop")
        return None
    if refused or not loops:
        if not loops and not refused:
            print(f"no loops configured in {config.config_dir()}")
        return None
    return loops[0]


def cmd_regression(args) -> int:
    """Link a later bug to the approved PR that introduced it (#661), or retract that link.

    The operator says which PR caused the bug; the loop only records what GitHub confirms: the
    issue is an issue, the PR merged, the issue was opened after the merge (an older bug is a
    pre-existing defect), and the loop's reviewer approved the merged head itself (an approval of
    an earlier head does not cover what merged). The model is that review's, from the run
    ledger. Exit 1 when refused, with the reason; 2 when GitHub or the ledger cannot answer."""
    from .. import gate, run_supervisor
    from ..stats import _iso
    loop = _one_loop(args)
    if loop is None:
        return 2
    say = f"#{args.issue} ← PR #{args.pr}"
    sup = run_supervisor.Supervisor(run_supervisor.production_ledger())
    if args.retract:
        if sup.retract_regression(loop["repo"], args.issue, args.pr):
            print(f"{say}: link retracted (kept in the ledger as retracted)")
            return 0
        print(f"{say}: refused — no live link to retract")
        return 1
    issue = gh.api(loop, f"/repos/{loop['repo']}/issues/{args.issue}", login=loop["read_token"])
    pr = gh.pr(loop, args.pr)
    if not isinstance(issue, dict) or issue.get("number") != args.issue:
        print(f"{say}: the issue could not be read from GitHub — nothing recorded")
        return 2
    if not isinstance(pr, dict) or pr.get("number") != args.pr:
        print(f"{say}: the PR could not be read from GitHub — nothing recorded")
        return 2
    if "pull_request" in issue:
        print(f"{say}: refused — #{args.issue} is a pull request, not a bug report")
        return 1
    merged, opened = _iso(pr.get("merged_at")), _iso(issue.get("created_at"))
    if merged is None:
        print(f"{say}: refused — PR #{args.pr} did not merge, so it cannot have shipped the bug")
        return 1
    if opened is None or opened < merged:
        print(f"{say}: refused — #{args.issue} was opened before PR #{args.pr} merged: a "
              "pre-existing defect, not one this approval let through")
        return 1
    head = str((pr.get("head") or {}).get("sha") or "")
    reviews = gh.reviews(loop, args.pr)
    if not isinstance(reviews, list):
        print(f"{say}: the reviews could not be read from GitHub — nothing recorded")
        return 2
    latest = gate.latest_effective_review_at_head(reviews, loop, head) if head else None
    if not latest or gh.review_state(latest) != "APPROVED":
        print(f"{say}: refused — the loop's reviewer did not approve PR #{args.pr}'s merged head "
              f"{head[:7] or '?'}; an approval of an earlier head does not cover what merged")
        return 1
    review_id = int(latest["id"])
    model = sup.approving_model(loop["repo"], args.pr, head, review_id)
    outcome = sup.link_regression(loop["repo"], args.issue, args.pr, head, review_id, model)
    print(f"{say}: {outcome} — approved at {head[:7]} (review {review_id}, model "
          f"{model or 'unknown'})")
    return 0


def cmd_review(args) -> int:
    """Thin I/O wrapper for the shared host-owned review operation."""
    from .. import one_shot_action
    loop = _one_loop(args)
    if loop is None:
        return 2
    result = one_shot_action.perform_review(loop, args.pr, getattr(args, "another_round", False))
    for line in result["lines"]:
        print(line)
    return result["code"]


def cmd_explain(args) -> int:
    """Why one PR is not moving, and the one event that would move it.

    Read-only all the way down: it reads GitHub and the loop's own state files and writes neither —
    no claim, no queue entry, no drain, no webhook POST, no token printed. The conclusions come
    from ``gate.explain``, so they are the predicates the live gates run rather than a second
    opinion about them.

    Exit 2 only when the question cannot be asked at all: an unknown loop, a loop file the loader
    refuses (without ``--loop`` each is named on a ``skipping <file>: <reason>`` line), several
    loops — refused ones included — and no ``--loop``, or no loop files at all. Before this function
    runs, argparse can also exit 2 for the same subcommand on its own account: a missing ``--pr``
    (``the following arguments are required: --pr``), an ``--pr`` value that is not an integer
    (``invalid int value``), an ``--pr`` flag with no value after it (``expected one argument``),
    an ``--loop`` flag with no value after it (``argument --loop: expected one argument``), or an
    unrecognized flag (``unrecognized arguments``).
    A PR GitHub does
    not have, or cannot be read, is an *answer*: it is reported as unknown, with the read to
    retry.
    """
    from .. import state as state_mod
    from ..run_supervisor import next_step

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
        skipped = [loop_id for loop_id, _ in refused]
        # A file that will not load is still a configured loop: the question may be about it.
        names = [loop["id"] for loop in loops] + skipped
        if len(names) > 1:
            print(f"{len(names)} loops are configured ({', '.join(names)}) — name one with --loop")
            return 2
        if skipped:
            return 2
        if not loops:
            print(f"no loops configured in {config.config_dir()}")
            return 2

    for loop in loops:
        st = state_mod.state_for(loop)
        report = gate.explain(loop, st, args.pr, gate.explain_facts(loop, args.pr))
        print()
        print(f"[{loop['id']}] {loop['repo']}#{args.pr} — why this PR is not moving")
        print(f"  {'pr:':<12}{report['url']}")
        print(f"  {'read:':<12}{report['read_at']} (GitHub pulls/reviews/hooks + local state; "
              f"read once, nothing written)")
        print(f"  {'state:':<12}{report['state_line']}")
        for issue_no, origin in sorted(st.fix_holds().items()):
            if origin == args.pr:
                print(f"  {'held:':<12}issue #{issue_no} fix waits for this PR to merge")
        from .. import fix_hold
        ask = st.fix_ask_open().get(args.pr)   # the number may be an issue waiting on a maintainer
        if ask:
            print(f"  {'held:':<12}{fix_hold.ask_line(args.pr, ask)}")
        if report['chain']['status'] != 'direct':
            print(f"  {'chain:':<12}{report['chain']['status']} · "
                  f"parents {report['chain']['parents']} · {report['chain']['reason']}")
        print(f"  {'budget:':<12}{report['budget']}")
        print(f"  {'seat:':<12}{report['seat']}")
        print(f"  {'queue:':<12}{report['queue']}")
        print(f"  {'in-flight:':<12}{report['inflight']}")
        for line in _dependency_lines(loop, args.pr, limit=3):
            print(f"  {'deps:':<12}{line}")
        # A head the host could not show whole cannot be approved by the loop (the broker refuses
        # it); only an operator can end it, so it is said here rather than left to the cap.
        for line in _view_lines(loop, args.pr, report.get("head")):
            print(f"  {'view:':<12}{line}")
        from .. import findings as findings_mod
        for text in findings_mod.lines(st.findings_get(args.pr)):
            print(f"  {'finding:':<12}{text}")
        print(f"  {'escalation:':<12}{report['escalation']}")
        print(f"  {'hooks:':<12}{report['hooks']}")
        print(f"  {'sweep:':<12}{report['sweep']}")
        if report.get("operator_agents"):                     # #757
            print(f"  {'operators:':<12}{', '.join(report['operator_agents'])} post as the "
                  "operator's account (operator agents): their comments reach the seats as "
                  "maintainer data, labelled as such")
        for text in report.get("authorship") or []:
            print(f"  {'authorship:':<12}{text}")
        print(f"  {'github:':<12}{report['github']}")
        decisions = report.get("gate_decisions") or []
        for text in decisions:
            print(f"  {'decided:':<12}{text}")
        if not decisions:
            print(f"  {'decided:':<12}no gate decision recorded for this PR")
        if not report.get("gate_failures"):
            print(f"  {'gates:':<12}no unresolved gate failure recorded for this PR")
        runs = _print_ledger_runs(loop, args.pr, f"  {'run:':<12}", limit=6)
        _print_shadows(loop, args.pr, f"  {'shadow:':<12}")
        _print_shadow_note(report, f"  {'shadow:':<12}")
        # A ledgered turn at the current head that waits to retry, failed before any write, or
        # is quarantined holds the PR as surely as any gate guard (#53): it is the blocker, and
        # its step is next. A failed turn that did write is final; GitHub shows where it left off.
        held = [row for row in runs if report.get("head") and row["head"] == report["head"]
                and (row["state"] != "failed" or row["write"] is None)]
        blockers = list(report["blockers"]) + [
            f"isolated {row['seat']} turn {row['state']} at this head — "
            f"{row['error'] or 'no reason recorded'}" for row in held]
        for text in blockers:
            print(f"  {'blocked:':<12}{text}")
        if not blockers:
            print(f"  {'blocked:':<12}nothing — no guard is holding this PR back")
        print(f"  {'next:':<12}"
              + (next_step(held[-1], loop["id"]) if held else report['next']['action']))
    return 0


def cmd_ready(args) -> int:
    """The open PRs that are safe to merge, in merge order, and why each other PR is left out (#479).

    Read-only: it reads GitHub and the loop's state files and writes neither; it never merges.
    Exit 2 only when the question cannot be asked: an unknown loop, a refused loop file, or several
    loops and no ``--loop``. A listing GitHub would not give is printed as an answer (unknown).
    """
    from .. import ready as ready_mod
    from .. import state as state_mod

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
        names = [loop["id"] for loop in loops] + [loop_id for loop_id, _ in refused]
        if len(names) > 1:
            print(f"{len(names)} loops are configured ({', '.join(names)}) — name one with --loop")
            return 2
        if refused:
            return 2
        if not loops:
            print(f"no loops configured in {config.config_dir()}")
            return 2
    for loop in loops:
        st = state_mod.state_for(loop)
        queue, excluded, error = ready_mod.collect(loop, st)
        print()
        print(f"[{loop['id']}] {loop['repo']} — ready to merge (read-only; the loop never merges)")
        if error:
            print(f"  unknown: the open PR list could not be read ({error})")
            continue
        if not queue:
            print("  ready:      none")
        for index, entry in enumerate(queue, 1):
            after = f" (after {', '.join(entry['after'])})" if entry["after"] else ""
            print(f"  {index}. #{entry['number']}{after} {entry['title']}")
            if entry["note"]:
                print(f"       {entry['note']}")
        for entry in excluded:
            print(f"  not ready: #{entry['number']} {entry['title']}")
            for why in entry["why"]:
                print(f"       - {why}")
    return 0


def cmd_drain(args) -> int:
    """Thin I/O wrapper for the shared host-owned drain operation."""
    from .. import one_shot_action
    loop = _one_loop(args)
    if loop is None:
        return 2
    result = one_shot_action.perform_drain(loop, args.seat)
    for line in result["lines"]:
        print(line)
    return result["code"]


def cmd_cleanup(args) -> int:
    try:
        config.load_id(args.loop)          # refuse a loop that will not load here, by name
    except config.ConfigError as exc:
        print(f"cannot clean up: {exc}")
        return 2
    cleanup = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "cleanup.py"
    cmd = [sys.executable, str(cleanup), "--loop", args.loop]
    cmd += ["--pr", str(args.pr)] if args.pr else ["--sweep"]
    if args.dry_run:
        cmd.append("--dry-run")
    return subprocess.run(cmd).returncode


# -- the parser for this module's commands (#676 step 7) --------------------------------------


def add_list(sub) -> None:  # noqa: ANN001
    """The ``list`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    sub.add_parser("list", help="List configured loops").set_defaults(func=cmd_list)


def add_status(sub) -> None:  # noqa: ANN001
    """The ``status`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    status = sub.add_parser("status", help="Show a loop's config and live state")
    status.add_argument("--loop", help="loop id (default: every configured loop)")
    status.set_defaults(func=cmd_status)


def add_review(sub) -> None:  # noqa: ANN001
    """The ``review`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    review = sub.add_parser("review", help="Ask for a fresh review of a PR's current head "
                                           "(the reviewer gate decides, as for a webhook)")
    review.add_argument("--loop", help="loop id (default: the only configured loop)")
    review.add_argument("--pr", type=int, required=True, help="the pull request to review")
    review.add_argument("--another-round", action="store_true",
                        help="allow exactly one more verdict on a review-only PR that has "
                             "reached its review cap (maintainer or operator only)")
    review.set_defaults(func=cmd_review)


def add_escalate(sub) -> None:  # noqa: ANN001
    """The ``escalate`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    escalate = sub.add_parser("escalate", help="Send one PR to the adjudicator now, whatever "
                                               "its verdict count (#459)")
    escalate.add_argument("--loop", help="loop id (default: the only configured loop)")
    escalate.add_argument("--pr", type=int, required=True, help="the pull request to escalate")
    escalate.add_argument("--reason", default="", help="why, for the ruling's record (one line)")
    escalate.add_argument("--withdraw", action="store_true",
                          help="cancel a pending escalation (one asked for while a run was in "
                               "flight, waiting for the next changes-requested verdict)")
    escalate.set_defaults(func=cmd_escalate)


def add_depends(sub) -> None:  # noqa: ANN001
    """The ``depends`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
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


def add_regression(sub) -> None:  # noqa: ANN001
    """The ``regression`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
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


def add_explain(sub) -> None:  # noqa: ANN001
    """The ``explain`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    explain = sub.add_parser("explain",
                             help="Why one PR is not moving, and what has to happen next")
    explain.add_argument("--loop", help="loop id (default: the only configured loop)")
    explain.add_argument("--pr", type=int, required=True, help="pull request number to explain")
    explain.set_defaults(func=cmd_explain)


def add_ready(sub) -> None:  # noqa: ANN001
    """The ``ready`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    ready = sub.add_parser("ready", help="List the open PRs that are safe to merge, in merge "
                                         "order, and why the others are not (read-only)")
    ready.add_argument("--loop", help="loop id (default: the only configured loop)")
    ready.set_defaults(func=cmd_ready)


def add_retry(sub) -> None:  # noqa: ANN001
    """The ``retry`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    retry = sub.add_parser("retry", help="Re-arm a PR's isolated run that failed before "
                                         "any GitHub write (refuses one that may have written)")
    retry.add_argument("--loop", required=True,
                       help="loop id (its config file name; `list` shows them)")
    retry.add_argument("--pr", type=int, required=True,
                       help="the pull request whose failed run to re-arm")
    retry.add_argument("--seat", choices=["reviewer", "fixer", "adjudicator", "triage",
                                          "issue_fixer", "shadow"],
                       help="only that seat's run (default: whichever failed at the PR's "
                            "newest head; a shadow run only when named)")
    retry.set_defaults(func=cmd_retry)


def add_continue(sub) -> None:  # noqa: ANN001
    """The ``continue`` subcommand's parser, registered by ``cli.register_cli`` (#485)."""
    cont = sub.add_parser("continue", help="Finish a half-written issue fix: inspect GitHub and "
                                           "complete only the missing stage (never pushes)")
    cont.add_argument("--loop", required=True,
                      help="loop id (its config file name; `list` shows them)")
    cont.add_argument("--run", required=True, help="the issue-fix run id (`status` lists it)")
    cont.add_argument("--dry-run", action="store_true",
                      help="only inspect and say what the next step would be")
    cont.set_defaults(func=cmd_continue)


def add_drain(sub) -> None:  # noqa: ANN001
    """The ``drain`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    drain = sub.add_parser("drain", help="Start a queued run once its seat is free")
    drain.add_argument("--loop", required=True,
                       help="loop id (its config file name; `list` shows them)")
    drain.add_argument("--seat", default="reviewer", choices=["reviewer", "fixer"],
                       help="which seat's queue to drain")
    drain.set_defaults(func=cmd_drain)


def add_cleanup(sub) -> None:  # noqa: ANN001
    """The ``cleanup`` subcommand's parser, registered by ``cli.register_cli`` (#676)."""
    cleanup = sub.add_parser("cleanup", help="Reclaim local disk for finished PRs")
    cleanup.add_argument("--loop", required=True,
                         help="loop id (its config file name; `list` shows them)")
    cleanup.add_argument("--pr", type=int,
                         help="clean one closed PR (default: sweep every closed PR the clone "
                              "knows about)")
    cleanup.add_argument("--dry-run", action="store_true",
                         help="list what would be removed, remove nothing")
    cleanup.set_defaults(func=cmd_cleanup)
