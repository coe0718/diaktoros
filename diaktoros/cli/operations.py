"""Day-to-day operating commands (#676): ``status``, ``list``, ``retry``, ``explain``, ``ready``,
``review``, ``escalate``, ``regression``, ``depends``, ``drain`` and ``cleanup``.

What the loop is doing and the operator's direct steps on one PR or issue. ``cli`` re-exports
every command here, so ``cli.cmd_status`` and the parser's dispatch are unchanged.
"""
from __future__ import annotations

import json
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
    state as state_mod,
)
from ..config import credentials
from .common import _budget_line, _credential_lines, _routes_of
from ..ledger import LOCK_WAIT_S
from ..util import logged


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
                                                         "observer_urgent")
                                            if role in _routes_of(loop)))
        refs = _credential_lines(loop)
        if refs:
            print("  token refs: " + " · ".join(refs))
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
                print(f"  running:    {seat} on {key} for {held:.0f}m")
        for line in _dependency_lines(loop):
            print(f"  deps:       {line}")
        for issue_no, origin in sorted(st.fix_holds().items()):
            print(f"  held:       issue #{issue_no} fix waits for PR #{origin} to merge")
        declared = st.issue_deps()
        for issue_no in st.dep_waits():
            names = ", ".join(f"#{a}" for a in sorted(declared.get(issue_no) or {})) or "?"
            print(f"  held:       issue #{issue_no} fix waits for prerequisite {names} "
                  f"(`hermes dk depends --loop {loop['id']} --issue {issue_no}` says why)")
        for pr_no, hold in sorted(st.pr_holds().items()):
            print(f"  held:       PR #{pr_no} {pr_hold.line(hold)}")
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


def _ledger_path() -> pathlib.Path:
    return config.host_path("ledger")


def _print_ledger_runs(loop: dict, pr: int | None, prefix: str, limit: int) -> list[dict]:
    """The isolated run ledger's failed/waiting/uncertain rows for this loop, read-only (#53).
    Prints them and returns them ([] when there is no readable ledger)."""
    from ..run_supervisor import describe_run, read_only_view, split_history

    ledger = _ledger_path()
    if not ledger.exists():
        return []
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


def cmd_retry(args) -> int:
    """Re-arm a PR's isolated run that failed before any external write (issue #53).

    Only runs at the PR's newest ledgered head are considered: the head of the most recently
    active run (``updated``), which is where the PR is after a backwards force-push re-armed an
    older head's run. A run that may have written —
    uncertain, quarantined, reconciled, or with a receipt claim, push intent or ruling on
    record — is refused with the reconcile instructions; it is never replayed.
    """
    from ..run_supervisor import Supervisor

    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"no such loop: {exc}")
        return 2
    ledger = _ledger_path()
    if not ledger.exists():
        print(f"no run ledger at {ledger} — nothing to retry")
        return 2
    sup = Supervisor(ledger)
    with sup._connect() as con:
        rows = [dict(row) for row in con.execute(
            "SELECT id,seat,head,turn_key,state,error,updated FROM runs WHERE repo=? AND pr=? "
            "ORDER BY created,id", (loop["repo"], args.pr))]
    if args.seat:
        rows = [row for row in rows if row["seat"] == args.seat]
    if not rows:
        print(f"[{loop['id']}] #{args.pr}: no isolated run on record"
              + (f" for the {args.seat} seat" if args.seat else ""))
        return 2
    # Not a timestamp: the claim path bumps `updated` when it supersedes a row for a head the PR
    # has since left, so `max(updated)` can name an abandoned head and offer nothing (#126).
    # The newest head that still has an offerable row is the ledger-only answer; the PR's own head
    # is the tiebreaker when a supersession bump makes those two disagree (a gh read, or — when
    # that read fails — the newest offerable head: never the rewritten timestamp).
    from ..run_supervisor import policy_cancelled
    from .. import gh

    def offerable(row: dict) -> bool:
        return row["state"] in ("failed", "waiting", "uncertain") or (
            row["state"] == "cancelled" and policy_cancelled(row["error"]))

    offerable_rows = [row for row in rows if offerable(row)]

    def newest(rws):
        return max(rws, key=lambda row: (row["updated"] or 0, row["id"]))["head"]

    ledger_head = newest(offerable_rows) if offerable_rows else newest(rows)
    bumped_head = newest(rows)
    # The PR read is only a tiebreaker: reach for it when a supersession bump made the two ledger
    # answers disagree. Otherwise stay on the ledger — no network round-trip for the common case
    # (and none when nothing is offerable: the "nothing to retry" path answers from the ledger).
    if offerable_rows and ledger_head != bumped_head:
        pr = gh.pr(loop, args.pr)
        pr_head = ((pr or {}).get("head") or {}).get("sha") if isinstance(pr, dict) else None
        head = pr_head or ledger_head
    else:
        head = ledger_head
    # A fixer run the push policy cancelled at claim is recovered here, under the policy in
    # force now; any other cancellation is superseded and is not offered (runs_view draws the
    # same line: a new head gets its own turn).
    candidates = [row for row in rows if row["head"] == head and offerable(row)]
    # A failed PR run whose PR the watchdog's last complete listing does not show open is history
    # (#723). The listing is trusted only when fresh and agreeing; otherwise one PR read confirms.
    from ..run_supervisor import WATCH_INTERVAL_S, ISSUE_SEATS, open_listing
    listing = open_listing(loop)
    if listing is not None and any(r["state"] == "failed" and r["seat"] not in ISSUE_SEATS
                                   for r in candidates):
        absent = args.pr not in listing["numbers"]
        if absent or time.time() - listing["at"] > WATCH_INTERVAL_S:
            live = gh.pr(loop, args.pr)
            if isinstance(live, dict):
                is_open = live.get("state") == "open"
            else:
                is_open = not absent          # unreadable: the listing alone decides
            if not is_open:
                print(f"[{loop['id']}] PR #{args.pr} is not open (merged or closed); nothing to re-arm")
                return 2
    if not candidates:
        print(f"[{loop['id']}] #{args.pr} @ {head[:7]}: nothing to retry — "
              + ", ".join(f"{row['seat']} {row['state']}" for row in rows if row["head"] == head))
        return 2
    rearmed, refused = 0, 0
    for row in candidates:
        label = f"{row['seat']} #{args.pr} @ {head[:7]}" + (f" ({row['turn_key']})" if row["turn_key"] else "")
        try:
            # The loop's budget now (#49): a turn killed at its budget reruns on the raised one.
            sup.retry(row["id"], budget=config.turn_budget(loop, row["seat"]))
        except ValueError as exc:
            refused += 1
            print(f"[{loop['id']}] {label} {row['state']}: {exc}")
            continue
        rearmed += 1
        print(f"[{loop['id']}] {label} re-armed (was {row['state']}: {row['error'] or 'no reason'})")
    if rearmed:
        sup.record_operator_action(loop["repo"], args.pr, "retry")
        try:
            started = gate.resume_isolated(loop)
        except Exception as exc:
            print(f"worker not started: {type(exc).__name__}: {exc} — the next event or armed "
                  "watchdog sweep starts it")
        else:
            print("worker started" if started else
                  "no private runtime file — the run stays pending until one exists")
    return 0 if rearmed and not refused else 2


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
    """How many reviewer runs the ledger holds for this head (read-only; 0 without a ledger)."""
    import sqlite3
    from .. import run_supervisor
    db = run_supervisor.production_ledger()
    if not db.exists():
        return 0
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
    try:
        return con.execute("SELECT COUNT(*) FROM runs WHERE repo=? AND pr=? AND head=? AND "
                           "seat='reviewer'", (repo, number, head)).fetchone()[0]
    finally:
        con.close()


def cmd_escalate(args) -> int:
    """Send one PR to the adjudicator now, whatever its verdict count (#459), as the operator.

    The same breach marker and isolated ruling turn a spent cap starts, marked ``escalated``: the
    PR is parked at this head as if its cap were spent (no further review or fix there), and the
    worker re-reads every fact before the ruling runs. Exit 1 when refused, with the reason;
    2 when the question cannot be asked.
    """
    from .. import run_supervisor, state as state_mod, transition
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
        if len(names) > 1:
            print(f"{len(names)} loops are configured ({', '.join(names)}) — name one with --loop")
            return 2
        if refused:
            return 2
        if not loops:
            print(f"no loops configured in {config.config_dir()}")
            return 2
        loop = loops[0]
    number = args.pr
    say = f"#{number}"
    if not str((loop.get("adjudicator") or {}).get("route") or ""):
        print(f"{say}: refused — this loop has no adjudicator; turn it on with "
              f"`hermes dk set --loop {loop['id']} --adjudicator-profile PROFILE`")
        return 1
    pr = gh.pr(loop, number)
    if not isinstance(pr, dict) or pr.get("number") != number:
        print(f"{say}: the PR could not be read from GitHub — nothing escalated")
        return 2
    head = str((pr.get("head") or {}).get("sha") or "")
    author = str((pr.get("user") or {}).get("login") or "").lower()
    say = f"#{number} @ {head[:7]}"
    if (pr.get("state") != "open" or pr.get("draft") or not head
            or (pr.get("base") or {}).get("ref") != loop["base"] or author not in loop["fixers"]):
        print(f"{say}: refused — only an open, ready PR by a fixer ({', '.join(loop['fixers'])}) "
              f"on {loop['base']} can be escalated")
        return 1
    st = state_mod.state_for(loop)
    reviews = transition.effective_reviews(loop, st, number, head, gh.reviews(loop, number))
    if not isinstance(reviews, list):
        print(f"{say}: the reviews could not be read — nothing escalated")
        return 2
    latest = gate.latest_effective_review_at_head(reviews, loop, head)
    if latest is not None and gh.review_state(latest) == "APPROVED":
        print(f"{say}: refused — approved at this head; there is nothing to rule on")
        return 1
    verdicts = len(gate.verdicts(reviews, loop))
    if verdicts < 1:
        print(f"{say}: refused — no verdict yet; there is nothing to rule on")
        return 1
    marker = st.breach_get(number)
    if isinstance(marker, dict) and marker.get("head") == head and marker.get("status") in gate.STANDING:
        print(f"{say}: already awaiting a ruling ({marker.get('status')})")
        return 1
    db = run_supervisor.production_ledger()
    if db.exists():
        import sqlite3
        # A queued review or fix is retired by the escalation itself (the worker's claim); only
        # a turn already running must finish first, since it may still write at this head.
        busy = run_supervisor.ACTIVE
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=LOCK_WAIT_S)
        try:
            out = con.execute(f"SELECT COUNT(*) FROM runs WHERE repo=? AND pr=? AND state IN "
                              f"({','.join('?' * len(busy))})", (loop["repo"], number, *busy)).fetchone()[0]
        finally:
            con.close()
        if out:
            print(f"{say}: refused — {out} run(s) for this PR are in flight; escalate once "
                  "they finish (a queued review or fix is retired by the escalation)")
            return 1
    why = " ".join(str(args.reason or "").split())[:200] or "the operator asked for a ruling"
    outcome = gate.breach(loop, st, number, head, verdicts, f"escalated: {why}",
                          escalated={"by": "operator", "at": now_iso(), "reason": why})
    if outcome == "stale":
        print(f"{say}: the PR moved while escalating — nothing escalated")
        return 1
    if outcome == "new":
        from ..run_supervisor import record_operator_action
        record_operator_action(loop["repo"], number, "escalate")
    print(f"{say}: escalated at {verdicts}/{loop['cap']} verdicts — adjudicator turn "
          f"{'enqueued' if outcome in ('new', 'retry') else outcome}; the PR is parked at this "
          f"head (`hermes dk explain --loop {loop['id']} --pr {number}` follows it)")
    return 0


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
    """Ask for a fresh review of a PR's current head (#375), as the operator.

    The real reviewer gate decides, fed a ``ready_for_review`` event built from the live PR: the
    same rules as a webhook (open, not a draft, a fixer's PR on the loop's base, no verdict or
    run already at this head, the verdict cap), so this command cannot start a review the gate
    would not. No GitHub write: the review itself is the only one, as usual. Exit 1 when the
    gate declines, with its own reason; 2 when the question cannot be asked.
    """
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
        if len(names) > 1:
            print(f"{len(names)} loops are configured ({', '.join(names)}) — name one with --loop")
            return 2
        if refused:
            return 2
        if not loops:
            print(f"no loops configured in {config.config_dir()}")
            return 2
        loop = loops[0]
    pr = gh.pr(loop, args.pr)
    if not isinstance(pr, dict) or pr.get("number") != args.pr:
        print(f"#{args.pr}: the PR could not be read from GitHub — nothing started")
        return 2
    head = (pr.get("head") or {}).get("sha") or ""
    from .. import state as state_mod
    st = state_mod.state_for(loop)
    key = gate.seat_key(loop, args.pr)
    if getattr(args, "another_round", False):
        # One more verdict on a review-only PR that hit its cap. The operator running this on the
        # host is the maintainer; the grant is recorded against this head and verdict count, so it
        # buys exactly one verdict and a repeat of the command changes nothing.
        author = ((pr.get("user") or {}).get("login") or "").lower()
        if author not in config.review_only(loop):
            print(f"#{args.pr}: {author or 'its author'} is not review-only — only a review-only "
                  "PR has a review cap to lift; nothing granted")
            return 1
        reviews = gh.reviews(loop, args.pr)
        if not isinstance(reviews, list):
            print(f"#{args.pr}: the reviews could not be read — nothing granted")
            return 2
        rounds = len(gate.verdicts(reviews, loop))
        if rounds < config.review_only_cap(loop):
            print(f"#{args.pr}: {rounds} of {config.review_only_cap(loop)} verdicts spent — the "
                  "cap is not reached; nothing to grant")
            return 1
        if gate.reviewed_at_head(reviews, loop, head):
            # The gate declines a head that already has a verdict before it looks at the cap, so a
            # grant here would be spent on nothing and not match the author's next push.
            print(f"#{args.pr} @ {head[:7]}: head already has a verdict — push the change, then "
                  "grant another round; nothing granted")
            return 1
        if not st.review_cap_grant(args.pr, head, rounds):
            print(f"#{args.pr} @ {head[:7]}: another round is already granted for this head")
        else:
            print(f"#{args.pr} @ {head[:7]}: another round granted — one more verdict")

    def queued():
        entry = (st.queue_all().get("reviewer") or {}).get(key) or {}
        return entry if entry.get("head") == head else None
    before = (_reviewer_runs(loop["repo"], args.pr, head), queued())
    payload = {"action": "ready_for_review", "number": args.pr, "pull_request": pr,
               "repository": {"full_name": loop["repo"]},
               "sender": {"login": loop.get("read_token") or "operator"}}
    script = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "gate_reviewer.py"
    try:
        done = subprocess.run([sys.executable, str(script)], input=json.dumps(payload),
                              text=True, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        print(f"#{args.pr} @ {head[:7]}: the gate did not finish within 120s — "
              "no answer; check `explain` before asking again")
        return 2
    if _reviewer_runs(loop["repo"], args.pr, head) > before[0]:
        from ..run_supervisor import record_operator_action
        record_operator_action(loop["repo"], args.pr, "review")
        print(f"#{args.pr} @ {head[:7]}: review queued for the isolated worker "
              f"(`hermes dk explain --loop {loop['id']} --pr {args.pr}` follows it)")
        return 0
    waiting = queued()
    if waiting and waiting != before[1]:
        from ..run_supervisor import record_operator_action
        record_operator_action(loop["repo"], args.pr, "review")
        reason = waiting.get("reason") or "the reviewer seat is busy"
        print(f"#{args.pr} @ {head[:7]}: review queued, waiting — {reason}")
        return 0
    reasons = logged(done.stderr.splitlines())
    print(f"#{args.pr} @ {head[:7]}: no review started — "
          + (reasons[-1] if reasons else "the reviewer gate declined without a reason"
             + (f" (exit {done.returncode})" if done.returncode else "")))
    return 1


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
        print(f"  {'github:':<12}{report['github']}")
        decisions = report.get("gate_decisions") or []
        for text in decisions:
            print(f"  {'decided:':<12}{text}")
        if not decisions:
            print(f"  {'decided:':<12}no gate decision recorded for this PR")
        if not report.get("gate_failures"):
            print(f"  {'gates:':<12}no unresolved gate failure recorded for this PR")
        runs = _print_ledger_runs(loop, args.pr, f"  {'run:':<12}", limit=6)
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
    try:
        config.load_id(args.loop)          # the watchdog reports a bad file but exits 0 (cron)
    except config.ConfigError as exc:
        print(f"cannot drain: {exc}")
        return 2
    watchdog = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "watchdog.py"
    cmd = [sys.executable, str(watchdog), "--loop", args.loop, "--drain", "--seat", args.seat]
    return subprocess.run(cmd).returncode


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
                                          "issue_fixer"],
                       help="only that seat's run (default: whichever failed at the PR's "
                            "newest head)")
    retry.set_defaults(func=cmd_retry)


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
