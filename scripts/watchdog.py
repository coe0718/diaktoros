#!/usr/bin/env python3
"""Stall watchdog — the loop's janitor, and the only thing that assumes nothing.

An unattended loop fails by going quiet, and "quiet" is indistinguishable from "nothing to
do". So this reads GitHub state directly instead of trusting pings, markers or logs, and it
names four shapes of stall:

1. the reviewer never posted a verdict for a head that has been sitting there;
2. the fixer never pushed after a verdict (or is held because the loop has not opted in to
   unattended fixer pushes — reported once per head, with the command that enables them);
3. a PR is parked awaiting adjudication;
4. the cap is spent at this head with no approval and no escalation marker.

It also reports stuck *seats* — a lock older than a run could plausibly live, or a request
that has been waiting — and drains whatever queued once its seat is free. A drained request
is re-checked against GitHub before it fires, so a stale queue entry dies instead of
starting a run against a head that has moved on.

Runs from cron (no agent, no tokens). Silent when the loop is paused: a parked loop must
never spend a run, and a watchdog that cries wolf on a deliberate pause gets ignored. *Not*
silent when it cannot tell: a hook list or ``/user`` it cannot read (a dead token, a 5xx, no
network) is an alert naming the login and status, re-raised every cooldown, while the parts
that need no GitHub read (route self-heal, ledger notices, observer retries) keep running. Each
sweep also warns before the read token's expiry date.

    watchdog.py                       # every configured loop
    watchdog.py --loop name           # one loop
    watchdog.py --loop name --drain --seat reviewer     # start a queued run, nothing else
    DIAKTOROS_TEST=1 watchdog.py     # ignore the paused check, zero grace (real data)
"""

from __future__ import annotations

import argparse
import fcntl
import os
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from diaktoros import conflict_person  # noqa: E402
from diaktoros import escalate_pending  # noqa: E402
from diaktoros import merge_check  # noqa: E402
from diaktoros import shadow_issues  # noqa: E402
from diaktoros import review_kick  # noqa: E402
from diaktoros import (  # noqa: E402
    ci_fix,
    config,
    envnames,
    fix_hold,
    gate,
    gate_failures,
    gate_shims,
    gh,
    hostdirs,
    main_check,
    ready,
    observer,
    pr_hold,
    route_intent,
    routes,
    ruled_approval,
    run_supervisor,
    stale_approval,
    state as state_mod,
    transition,
    triage_sweep,
    watch_conflicts,
    watch_health,
    watch_mode,
    watch_stacked,
)
from diaktoros.util import age_min, epoch, log, now_iso  # noqa: E402

# #532: how long a head may sit with a pending, unannounced review request before the sweep
# delivers it to the gate itself (one sweep interval).
KICK_AFTER_MIN = 15
PUSH_OFF_KIND = "fixer held — unattended fixer pushes are off"
HEAD_RETENTION_SEC = 30 * 86400  # retain absent PRs long enough for transient listing/state changes
SCRIPTS = pathlib.Path(__file__).resolve().parent
# A hanging GitHub must not stall the sweep (#75): every read is capped, and the whole run has
# a budget; when it is spent the sweep stops, says so, and the next cron run starts fresh.
WATCHDOG_BUDGET_S = 600.0
WATCHDOG_PER_CALL_S = 20.0


def watchdog_budget() -> float:
    try:
        value = float(envnames.get("WATCHDOG_BUDGET_S", WATCHDOG_BUDGET_S))
    except ValueError:
        value = WATCHDOG_BUDGET_S
    return value if 0 < value <= 86400 else WATCHDOG_BUDGET_S


# How a gate-failure alert is said. ``main`` prints and flushes each one as it is made, so the
# ledger marks an alert only once it has actually been written out (a sweep killed before that
# leaves the entry's claim to expire and a later sweep says it). In-process callers (tests)
# leave it None and get the lines back instead.
EMIT = None


def sweep_gate_failures(ledger: gate_failures.Ledger, header: str, cooldown_s: float,
                        may_redrive: bool, held: str = "") -> list[str]:
    """A gate that crashed, overran, or silenced after a failed read (#75): alert, re-drive."""
    try:
        return gate_failures.sweep(ledger, header, SCRIPTS, cooldown_s=cooldown_s,
                                   may_redrive=may_redrive, held=held, emit=EMIT)
    except Exception as exc:                      # never let this hide the stall scan
        return [f"⚠️ Diaktoros {header} gate-failure sweep failed: {type(exc).__name__}: {exc}"]


# -- draining ------------------------------------------------------------------


# The drain itself lives in diaktoros/drain.py (#1047): the sweep, ``--drain`` and the host's
# confirmed drain share one implementation. Bound here so ``watchdog.drain`` keeps working.
from diaktoros.drain import drain  # noqa: E402


# -- the sweep ------------------------------------------------------------------


def drain_queued(loop: dict, st: state_mod.LoopState, lines: list[str]) -> None:
    """Free capacity is a scheduling signal, not a stall notification."""
    for seat in ("reviewer", "fixer"):
        if drain(loop, st, seat, quiet=True):
            lines.append(f"started the queued {seat} run whose wait was over")


def record_overrides(loop: dict, st) -> None:
    """Record the merges over a standing changes-requested (#220). Never fails the sweep."""
    try:
        from diaktoros import overrides, run_supervisor
        for number in overrides.scan(loop, st, run_supervisor.Supervisor(
                run_supervisor.production_ledger())):
            log(f"#{number}: merged over a changes-requested; override recorded")
    except Exception as exc:
        log(f"override scan: {type(exc).__name__}: {exc}")


def send_weekly_report(loop: dict, st) -> None:
    """The loop's weekly report, when one is due (#658). Never fails the sweep."""
    try:
        from diaktoros import run_supervisor, stats
        stats.weekly(loop, st, run_supervisor.production_ledger())
    except Exception as exc:
        log(f"weekly report: {type(exc).__name__}: {exc}")


def retry_pending_breaches(loop: dict, st: state_mod.LoopState, prs: list,
                           failures: list | None = None) -> None:
    """Retry listed eligible heads only after reviews verify the cap.

    The first armed sweep baselines stall clocks, not pending delivery. The
    breach gate rechecks the live head under its delivery lock before POST.
    """
    markers = st.breach_all()
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("state") != "open" or pr.get("draft"):
            continue
        if (pr.get("base") or {}).get("ref") != loop["base"]:
            continue
        number = pr.get("number")
        head = (pr.get("head") or {}).get("sha")
        if type(number) is not int or not head:
            continue
        marker = markers.get(f"{loop['repo']}#{number}")
        # The authors gate.breach parks (shadow on #1154): an operator's or a shadow's escalation
        # covers every reviewed author, review-only included; a spent cap only a fixer's PR.
        by = ((marker.get("escalated") or {}).get("by") if isinstance(marker, dict) else None)
        authors = (config.reviewed_authors(loop) if by in {gate.SHADOW_ESCALATION, "operator"}
                   else set(loop["fixers"]))
        if ((pr.get("user") or {}).get("login") or "").lower() not in authors:
            continue
        if (not isinstance(marker, dict) or gate.breach_delivery_status(marker, head) != "delivery-pending"
                or marker.get("head") != head):
            continue
        # record() erased the pre-retarget marker; a marker at a held head can only come from
        # receipted post-boundary verdicts, and only those may re-verify its cap.
        errors: list[str] = []
        reviews = transition.effective_reviews(loop, st, number, head,
                                               gh.reviews(loop, number, errors=errors))
        if not isinstance(reviews, list):
            if failures is not None:
                failures.extend((number, e) for e in errors[:1])
            continue
        latest = gate.latest_effective_review_at_head(reviews, loop, head)
        if latest is not None and gh.review_state(latest) == "APPROVED":
            continue
        changes = gate.verdicts(reviews, loop)
        if not gate.breach_holds(marker, head, len(changes), loop["cap"]):
            # A raised cap or a dismissed verdict: the head is no longer parked, so the stale
            # marker must not hide it from the stall scan. Clear it (#780).
            if st.breach_clear_pending(number, head):
                log(f"#{number} @ {head[:7]}: pending breach no longer holds "
                    f"({len(changes)}/{loop['cap']} verdicts) — cleared")
            continue
        gate.breach(loop, st, number, head, marker.get("rounds", len(changes)),
                    marker.get("reason", "review cap reached"),
                    escalated=marker.get("escalated") if gate.escalated(marker, head) else None)


def sweep_loop(loop: dict, st: state_mod.LoopState, lines: list[str] | None = None) -> list[str]:
    """One sweep per loop at a time: a non-blocking flock held from before the first read of
    ``st.watch()`` until the last save. A sweep that finds it held skips the loop (no retry)."""
    lines = [] if lines is None else lines
    hostdirs.ensure(st.dir)
    fd = os.open(st.dir / "sweep.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"sweep already running for {loop.get('id', '?')}")
            return lines
        try:
            return _sweep_loop_locked(loop, st, lines)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _sweep_loop_locked(loop: dict, st: state_mod.LoopState, lines: list[str]) -> list[str]:
    # Filled in place, so the lines a failing sweep already produced (a gate-failure alert
    # among them) still reach the operator next to the error.
    lines = [] if lines is None else lines
    c = _Sweep(loop, st, lines)
    if not _hooks_armed(c):
        return lines
    _read_listing(c)
    _heal_and_redrive(c)
    if not isinstance(c.prs, list):
        _no_listing(c)
        return lines
    _recover(c)
    _observe_heads(c)
    retry_pending_breaches(loop, st, c.prs, c.pr_failures)
    if c.first_sweep:
        _first_sweep_done(c)
        return lines
    _scan_stalls(c)
    _stuck_states(c)
    _report_stalls(c)
    _save_alerts(c)
    drain_queued(loop, st, lines)
    _notify_stalls(c)
    _best_effort_sweeps(c)
    c.conflicts += watch_conflicts.recheck_unknown(loop, c.unknown, c.conflict_live)
    watch_conflicts.act_on_conflicts(loop, st, c.conflicts, c.conflict_live)
    _finish(c)
    return lines


class _Sweep:
    """One sweep's state, read and written by its steps in order (#1349): the loop, its watch
    state and the clock taken first, the open-PR listing and the health check, and what the
    stall scan collects for the report, the cooldown map and the observer."""

    def __init__(self, loop: dict, st: state_mod.LoopState, lines: list[str]):
        self.loop, self.st, self.lines = loop, st, lines
        self.watch = st.watch()
        self.now = time.time()
        self.cooldown = 0.0 if watch_mode.TEST else float(loop.get("cooldown_h") or 6) * 3600
        self.health: watch_health.Health | None = None
        self.listing_errors: list[str] = []
        self.pr_failures: list[tuple[int, str]] = []  # per-PR review reads that failed
        self.prs = None

    def due(self, key: str) -> bool:
        """The cooldown map (#77): ``marks`` is when each still-standing stall last alerted. A
        key is stamped only when it alerts, never merely for being seen, so a stall that persists
        re-alerts every cooldown; ``present`` is what this sweep still sees, and a key it no
        longer sees has cleared and is dropped, so the same stall returning alerts at once."""
        self.present.add(key)
        last = watch_mode.valid_clock(self.marks.get(key), self.now)
        if last is not None and self.now - last < self.cooldown:
            return False
        self.raised[key] = self.now
        return True

    def finish_reads(self) -> None:
        watch_health.finish_reads(self.loop, self.watch, self.now, self.health, self.pr_failures,
                                  self.lines)


def _hooks_armed(c: _Sweep) -> bool:
    loop = c.loop
    c.armed, c.armed_error = (True, "") if watch_mode.TEST else gate.hooks_read(loop)
    if c.armed is False:
        # Parked on purpose: no stall is reported and nothing is re-driven. A gate failure
        # already recorded is still said — it is a fact about a delivery, not a stall.
        if loop.get("state_dir"):
            c.lines.extend(sweep_gate_failures(
                gate_failures.loop_ledger(loop), f"[{loop['id']}] {loop['repo']}", c.cooldown,
                may_redrive=False,
                held="not re-driven while the loop's hooks are paused; `hermes dk arm "
                     f"--loop {loop['id']}` resumes it, or re-deliver it from GitHub"))
        return False
    # Armed, or unknown because the hook list could not be read. Unknown is not paused: sweep
    # normally and warn once per cooldown — never silence.
    if c.armed is None and watch_mode.alert_due(c.watch, "hooks:unknown", c.now, c.cooldown):
        c.lines.append(f"⚠️ Diaktoros [{loop['id']}] {loop['repo']}: hook state unreadable: "
                       f"{gh.one_line(c.armed_error or 'no reason given', 200)} — sweeping anyway")
    return True


def _read_listing(c: _Sweep) -> None:
    # With the hooks confirmed, the open-PR listing is read here, before the health check, so
    # that a listing GitHub refuses counts as a failed read in the same sweep (a /user probe
    # that works must not announce "reads work again" while the listing is still refused).
    c.prs = gh.open_prs(c.loop, errors=c.listing_errors)
    if not watch_mode.TEST:
        c.health = watch_health.read_health(c.loop, c.st, c.watch, c.now, c.armed, c.armed_error,
                                            c.listing_errors[0] if c.listing_errors else "")
        c.lines.extend(c.health.lines)


def _heal_and_redrive(c: _Sweep) -> None:
    loop = c.loop
    # Self-heal first, and independent of GitHub listing: a route another registry writer erased
    # or rewrote (issue #1) is a loop that cannot wake a seat, whatever the PRs look like.
    try:
        healed = route_intent.heal(loop) + gate_shims.heal(loop) + gate_shims.heal_watchdog_shim()
    except Exception as exc:                      # never let the heal hide the stall scan
        healed = [f"⚠️ Diaktoros [{loop['id']}] route self-heal failed: "
                  f"{type(exc).__name__}: {exc}"]
    if healed:
        c.lines.extend(healed)
        c.st.note("route self-heal: " + " | ".join(line.strip() for line in healed))

    # Re-drive only while GitHub answers: a re-run during an outage would only fail again.
    # (The listing was read above, before the health check.)
    if loop.get("state_dir"):
        c.lines.extend(sweep_gate_failures(
            gate_failures.loop_ledger(loop), f"[{loop['id']}] {loop['repo']}", c.cooldown,
            may_redrive=isinstance(c.prs, list)))

    # Issue #53: a pre-write failure waits out its backoff in the run ledger, and a claim
    # whose GitHub read failed stays pending. Nothing but an event would relaunch either, so
    # an armed sweep runs the worker-enabled recovery (a no-op without a private runtime).
    try:
        gate.resume_isolated(loop)
    except Exception as exc:
        c.lines.append(f"⚠️ Diaktoros [{loop['id']}] isolated retry scheduling failed: "
                       f"{type(exc).__name__}: {exc}")


def _no_listing(c: _Sweep) -> None:
    # Without a complete listing, even individually readable PRs cannot establish
    # that the sweep's scheduling view is current. Explicit --drain still rechecks.
    why = f" ({gh.one_line(c.listing_errors[0], 200)})" if c.listing_errors else ""
    listing_said = bool(c.listing_errors) and c.health is not None and c.health.alerted
    if not listing_said:                  # the health alert above already said it, with why
        c.lines.append(f"⚠️ {c.loop['id']}: could not list open PRs{why} — stall scan and queue "
                       f"drain skipped this run")
    c.finish_reads()
    c.st.watch_save(c.watch)


def _recover(c: _Sweep) -> None:
    loop, st, prs, lines = c.loop, c.st, c.prs, c.lines
    # The last COMPLETE open-PR listing, saved with the sweep's watch state, so status,
    # explain and retry can tell a failed run on a closed PR from one on an open PR (#723).
    c.watch["open_prs"] = {"at": c.now, "numbers": sorted(
        p["number"] for p in prs if isinstance(p, dict) and isinstance(p.get("number"), int))}
    retire_stale_runs(loop, prs, lines)
    try:
        lines.extend(fix_hold.sweep(loop, st))
    except Exception as exc:
        lines.append(f"⚠️ Diaktoros [{loop['id']}] held issue fixes: "
                     f"{type(exc).__name__}: {exc}")
    try:
        lines.extend(triage_sweep.sweep(loop, st))
    except Exception as exc:                  # never let the recovery hide the stall scan
        lines.append(f"⚠️ Diaktoros [{loop['id']}] triage recovery sweep: "
                     f"{type(exc).__name__}: {exc}")
    watch_stacked.reconcile_stacked(loop, st, c.watch, prs, lines)
    watch_stacked.retry_fresh_reviews(loop, st, prs, lines, since=c.now)


def _observe_heads(c: _Sweep) -> None:
    watch, now = c.watch, c.now
    # A commit's authored/committed date says nothing about when its SHA reached a PR.
    # Snapshot the heads on the first *successful* armed sweep, before any stall evaluation.
    # Those heads are history; later SHA changes get their own durable observation clock.
    # A malformed persisted arming clock has no trustworthy ordering against PR
    # creation or prior observations. Re-arm only after this successful listing and
    # baseline its heads; never coerce strings/bools or reuse old grace clocks.
    c.first_sweep = first_sweep = watch_mode.valid_clock(watch.get("armed_since"), now) is None
    if first_sweep:
        watch["armed_since"] = now
    history = watch.get("head_history")
    watch["head_history"] = ({k: v for k, v in history.items()
                              if isinstance(v, dict) and
                              (clock := watch_mode.valid_clock(v.get("last_seen_at"), now))
                              is not None and
                              now - clock < HEAD_RETENTION_SEC}
                             if isinstance(history, dict) else {})
    c.current_heads = current_heads = _kept_heads(c)
    for pr in c.prs:
        if not isinstance(pr, dict):
            continue
        author = ((pr.get("user") or {}).get("login") or "").lower()
        if author not in config.reviewed_authors(c.loop) or pr.get("draft"):
            continue
        number = pr.get("number")
        head = (pr.get("head") or {}).get("sha") or ""
        if not number or not head:
            continue
        key = str(number)
        previous = current_heads.get(key, {})
        base = (pr.get("base") or {}).get("ref")
        base_sha = (pr.get("base") or {}).get("sha")
        # For a direct-trunk PR, trunk moving on is not a new situation: only its own head or
        # base ref restarts the stall clock. A stacked base's generation does.
        if (previous.get("sha") == head and previous.get("base") in (None, base)
                and (base == c.loop["base"] or previous.get("base_sha") in (None, base_sha))):
            current_heads[key] = {**previous, "base": base, "base_sha": base_sha,
                                  "last_seen_at": now}
        else:
            if previous:
                history = watch.setdefault("head_history", {})
                history[f"{key}:{previous.get('sha')}:{previous.get('base', '')}:{previous.get('base_sha', '')}"] = previous
            # A first-seen old PR could be preexisting; created_at only establishes
            # eligibility for genuinely new PRs, never the time of a later push.
            new_pr = epoch(pr.get("created_at")) >= int(watch["armed_since"])
            current_heads[key] = {"sha": head, "base": base, "base_sha": base_sha, "observed_at":
                                  now if previous or (not first_sweep and new_pr) else None,
                                  # When the loop first saw this head, whatever the PR's age: the
                                  # merge check's missing-check grace starts here (#1236).
                                  "first_seen_at": now, "last_seen_at": now}
    watch["heads"] = current_heads
    c.st.watch_save(watch)               # persist observations even if review reads fail


def _kept_heads(c: _Sweep) -> dict[str, dict]:
    """The heads the last sweep saw, still within retention (none on a first sweep)."""
    now = c.now
    heads = {} if c.first_sweep else c.watch.get("heads")
    if not isinstance(heads, dict):
        heads = {}
    current_heads: dict[str, dict] = {}
    for key, previous in heads.items():
        if not isinstance(previous, dict) or not previous.get("sha"):
            continue
        # Older state has no last_seen_at. Give it one bounded retention window
        # instead of erasing a live observation during the schema transition.
        last_seen = watch_mode.valid_clock(previous.get("last_seen_at"), now) or now
        if now - last_seen < HEAD_RETENTION_SEC:
            current_heads[key] = {"sha": previous["sha"],
                                  "base": previous.get("base"),
                                  "base_sha": previous.get("base_sha"),
                                  "observed_at": watch_mode.valid_clock(
                                      previous.get("observed_at"), now),
                                  "first_seen_at": watch_mode.valid_clock(
                                      previous.get("first_seen_at"), now),
                                  "last_seen_at": last_seen}
    return current_heads


def _first_sweep_done(c: _Sweep) -> None:
    loop, st = c.loop, c.st
    st.note("loop observed armed — head snapshot set; existing heads excluded")
    drain_queued(loop, st, c.lines)
    if observer.retry(loop, st):
        log("observer: retried an undelivered notice")
    observer.flush(loop, st, wait_s=0 if watch_mode.TEST else observer.digest_wait(loop))
    record_overrides(loop, st)
    send_weekly_report(loop, st)
    c.finish_reads()
    st.watch_save(c.watch)


def _scan_stalls(c: _Sweep) -> None:
    loop, st, watch = c.loop, c.st, c.watch
    # Per seat (#98): each seat's stall waits for its own whole turn, never another seat's.
    c.grace = {seat: 0.0 if watch_mode.TEST else config.stall_grace_s(loop, seat) / 60
               for seat in ("reviewer", "fixer")}
    c.marker_grace = 0.0 if watch_mode.TEST else float(loop.get("marker_grace_min") or 60)
    c.breach = st.breach_all()
    c.alerts: list[tuple[int, str, str]] = []
    c.marks = watch.get("alerts") if isinstance(watch.get("alerts"), dict) else {}
    c.present: set[str] = set()
    c.unjudged: set[str] = set()          # PRs whose reviews were unreadable: not "cleared"
    c.raised: dict[str, float] = {}
    c.conflicts: list[tuple[int, str, str, str]] = []
    c.conflict_live: dict[int, dict] = {}
    c.unknown: list[tuple[int, str, str]] = []     # mergeability not computed yet at this read
    # #532: heads whose pending review request the sweep already delivered to the gate itself.
    kicks = watch.get("review_kicks") if isinstance(watch.get("review_kicks"), dict) else {}
    open_numbers = {str(p.get("number")) for p in c.prs if isinstance(p, dict)}
    c.kicks = {k: v for k, v in kicks.items() if k in open_numbers}
    for pr in c.prs:
        if (listed := config.loop_pr(loop, pr)) is None:
            continue
        _judge_pr(c, pr, *listed)


def _judge_pr(c: _Sweep, pr: dict, number: int, head: str, author: str) -> None:
    """One loop PR in the stall scan: its hold, its mergeability, then its stall if any."""
    loop, st = c.loop, c.st
    if _held(c, number):
        return
    # #303 stage 1: a loop PR that no longer merges into its base is said once per head.
    # The listing carries no mergeability, so this is one read of the PR; GitHub computes it
    # lazily, and an unknown answer (null) is simply not a conflict yet.
    live = gh.pr(loop, number)
    verdict = watch_conflicts.mergeability(loop, live, head, author)
    if verdict == "conflict":
        c.conflicts.append((number, head, str((live.get("base") or {}).get("sha") or ""), author))
        c.conflict_live[number] = live
    elif verdict == "unknown":
        c.unknown.append((number, head, author))

    # A held head is judged only by host-receipted post-boundary reviews: an old verdict
    # is not a current stall, and a missing fresh verdict is the reviewer's to post.
    boundary = transition.hold(st, number, head)
    if boundary and transition.baseline_missing(boundary):
        return  # permanently held; explain reports why, no seat can move it
    errors: list[str] = []
    reviews = transition.effective_reviews(loop, st, number, head,
                                           gh.reviews(loop, number, errors=errors))
    if not isinstance(reviews, list):
        # Unknown beats wrong: no verdict is guessed — but the skipped check is said.
        c.pr_failures.extend((number, e) for e in errors[:1])
        c.unjudged.add(f"{number}:")      # nor is its stall cleared: its cooldown stands
        return
    latest = gate.latest_effective_review_at_head(reviews, loop, head)
    if latest is not None and gh.review_state(latest) == "APPROVED":
        _approved(c, pr, number, head, reviews)
        return                                    # approved at this head: the loop is done here
    kind = _stall_kind(c, pr, number, head, author, reviews, latest, boundary)
    if kind and c.due(stall_key(number, head, kind)):
        c.alerts.append((number, kind, (pr.get("title") or "")[:60]))


def _held(c: _Sweep, number: int) -> bool:
    """#477: a PR a maintainer holds is left alone: no stall, no conflict turn. The hold is
    recorded for `status` and the drain; a failed read keeps the last record (never
    "released" by a read that failed) and is said with the other failed reads."""
    loop, st = c.loop, c.st
    hold, hold_error = pr_hold.read(loop, number)
    if hold_error:
        c.pr_failures.append((number, hold_error))
    elif hold:
        if st.pr_hold_set(number, hold):              # a hold began: the report's (#678)
            from diaktoros.run_supervisor import record_operator_action
            record_operator_action(loop["repo"], number,
                                   "agent hold" if config.maintainer_kind(
                                       loop, hold.get("by")) == "operator agent" else "hold")
    else:
        st.pr_hold_drop(number)
    if hold or (hold_error and number in st.pr_holds()):
        c.unjudged.add(f"{number}:")
        return True
    return False


def _approved(c: _Sweep, pr: dict, number: int, head: str, reviews: list) -> None:
    loop, st = c.loop, c.st
    # #1004: unless the shadow review disagrees, then it goes for a ruling (once per
    # head; the shadow's own finish tries first, this is the backstop).
    try:
        if gate.shadow_escalation(loop, st, number, head, reviews) == "new":
            log(f"#{number} @ {head[:7]} shadow review disagrees with the approval: "
                "sent for a ruling")
    except Exception as exc:
        log(f"#{number} shadow escalation check failed: {type(exc).__name__}: {exc}")
    try:
        # #1004 part 3: an upheld ruling's fix, if the ruling's own delivery did not
        # queue it (same turn key, so never twice).
        if gate.queue_shadow_fix(loop, number, head) in gate.SCHEDULED_OUTCOMES:
            log(f"#{number} @ {head[:7]} upheld shadow review handed to the fixer")
    except Exception as exc:
        log(f"#{number} upheld shadow fix check failed: {type(exc).__name__}: {exc}")
    # #1114: the ruling it went for is owed like any other: a dead adjudicator turn is
    # a stall here too. A pending delivery is the escalation check's to retry (above).
    marker = st.breach_get(number)
    if ((marker.get("escalated") or {}).get("by") == gate.SHADOW_ESCALATION
            and gate.breach_delivery_status(marker, head) != "delivery-pending"):
        kind = parked_kind(loop, marker, number, head, c.marker_grace) or ""
        if kind and c.due(stall_key(number, head, kind)):
            c.alerts.append((number, kind, (pr.get("title") or "")[:60]))


def _stall_kind(c: _Sweep, pr: dict, number: int, head: str, author: str, reviews: list,
                latest: dict | None, boundary) -> str:
    """The stall a reviewed, unapproved loop PR is in at this head, or ``""`` for none."""
    loop, st = c.loop, c.st
    at_head = (gate.changes_at_head(reviews, loop, head)
               if latest is not None and gh.review_state(latest) == "CHANGES_REQUESTED"
               else [])
    changes = gate.verdicts(reviews, loop)
    marker = c.breach.get(f"{loop['repo']}#{number}") or {}
    # A marker that no longer holds (#780) parks nothing, ruled or not. One that holds with
    # its delivery failed is not a silent stall either: the retry owns it.
    holds = gate.breach_holds(marker, head, len(changes), loop["cap"])
    if holds and gate.breach_delivery_status(marker, head) == "delivery-pending":
        return ""
    observed_at = c.current_heads[str(number)]["observed_at"]
    head_postdates_arming = watch_mode.TEST or observed_at is not None
    kind = ""

    # A marker at this head is the escalation: whether it is a stall is its own question
    # (parked_kind), and it is never "no escalation marker" while it is young (#98).
    parked = parked_kind(loop, marker, number, head, c.marker_grace) if holds else None
    # A review-only PR has no fixer and no adjudication: changes requested is back with its author (#191).
    review_only = author in config.review_only(loop)
    if parked is not None:
        kind = parked
    elif not at_head and isinstance(st.human_hold(number, head), dict):
        pass                                  # human_paths: a person's approval (#478)
    elif review_only and at_head:
        pass                                  # the author's move, not a stall
    elif (review_only and len(changes) >= config.review_only_cap(loop)
          and not st.review_cap_granted(number, head, len(changes))):
        pass                                  # review cap reached: a maintainer's move
    elif len(changes) >= loop["cap"] and head_postdates_arming and not review_only:
        kind = _cap_spent_kind(c, number, head, len(changes))
    elif at_head and not config.unattended_fixer_push_enabled(loop):
        mins = age_min(latest.get("submitted_at"))
        if past(mins, c.grace["fixer"]):
            # Not a stall the fixer can end: no fixer turn starts until the loop opts in.
            kind = (f"{PUSH_OFF_KIND} — changes requested {ago(mins)} at head "
                    f"{head[:7]} waits for you: run "
                    f"`{config.fixer_push_enable_command(loop)}` (or fix it by hand)")
    elif at_head and (disputed := run_supervisor.views.disputed(
            run_supervisor.production_ledger(), loop["repo"], number, head, latest)) \
            and not live_turn(loop, number, head, "fixer"):
        kind = _disputed_kind(c, number, head, len(changes), latest, disputed)
    elif at_head:
        mins = age_min(latest.get("submitted_at"))
        # A fix turn queued or running at this head is not a stall: the grace covers a turn's
        # run, not its wait in the queue (behind another PR, paced, held).
        if past(mins, c.grace["fixer"]) and not live_turn(loop, number, head, "fixer"):
            kind = (f"fixer never pushed — changes requested {ago(mins)} at head "
                    f"{head[:7]} by {gate.reviewer_login(latest)}")
    elif config.fix_ci(loop) and ci_fix.pending_at(loop["repo"], number, head):
        pass                                  # #539: the fixer takes this red head first
    else:
        kind = _reviewer_kind(c, pr, number, head, author, boundary, observed_at,
                              head_postdates_arming)
    return kind


def _cap_spent_kind(c: _Sweep, number: int, head: str, spent: int) -> str:
    """#575: a spent cap with no marker at this head (the head moved after the cap fired, or no
    gate event ever reached it) is handed to adjudication here, exactly as the reviewer gate
    would: no model, no GitHub write. Only a failed hand-off is a stall. Without an adjudicator
    there is no one to hand it to: the alert is the operator's."""
    loop = c.loop
    handed = ""
    if (loop.get("adjudicator") or {}).get("route"):
        try:
            handed = gate.breach(loop, c.st, number, head, spent,
                                 f"review cap reached — {loop['cap']} verdicts, no "
                                 "approval (raised by the watchdog at this head)")
        except Exception as exc:
            log(f"#{number} @ {head[:7]}: cap spent, hand-off failed: "
                f"{type(exc).__name__}: {exc}")
    if handed == "new":
        log(f"#{number} @ {head[:7]}: cap spent with no marker here — handed to "
            "adjudication")
    elif handed != "stale":
        return (f"{spent} verdicts, no approval and NO escalation marker — "
                f"the cap may not have fired")
    return ""


def _disputed_kind(c: _Sweep, number: int, head: str, spent: int, latest: dict,
                   disputed: float) -> str:
    """The fixer answered the verdict with a dispute (#401): every finding "not a defect",
    nothing pushed. That is an answer, not a stall (#895). With an adjudicator it goes for a
    ruling, as `hermes dk escalate` would send it (#894): the head is parked, so a later sweep
    sees it awaiting a ruling. Without one, it is the operator's to decide."""
    loop = c.loop
    handed = ""
    if (loop.get("adjudicator") or {}).get("route"):
        try:
            handed = gate.breach(
                loop, c.st, number, head, spent,
                "escalated: the fixer disputed the review (every finding answered as "
                "not a defect, nothing pushed)",
                escalated={"by": "dispute", "at": now_iso(),
                           "reason": "the fixer disputed the review at this head"})
        except Exception as exc:
            log(f"#{number} @ {head[:7]}: dispute hand-off failed: "
                f"{type(exc).__name__}: {exc}")
    if handed == "new":
        log(f"#{number} @ {head[:7]}: the fixer disputed the review — handed to "
            "adjudication")
    elif handed not in ("retry", "stale") and past(age_min(latest.get("submitted_at")),
                                                   c.grace["fixer"]):
        return (f"disputed {ago((c.now - disputed) / 60)} at head {head[:7]} — the fixer "
                "answered every finding as no defect and pushed nothing; waiting for you: "
                + run_supervisor.views.dispute_choices(loop["id"], number)
                if not handed else
                f"disputed at head {head[:7]}: hand-off to adjudication {handed}")
    return ""


def _reviewer_kind(c: _Sweep, pr: dict, number: int, head: str, author: str, boundary,
                   observed_at: float | None, head_postdates_arming: bool) -> str:
    """No verdict at this head: deliver a pending request the gate never saw (#532), and say
    the reviewer's stall once its grace is past."""
    loop = c.loop
    mins = (c.now - observed_at) / 60 if observed_at is not None else 0.0
    # #532: a request GitHub never announced (the reviewer was already requested when the
    # fixer pushed) leaves the head with a pending request, no verdict and no run. After
    # one sweep interval, deliver that request to the gate once per head; the gate's own
    # rules and the ledger's dedup decide, so a held or queued review is not doubled.
    # A same-head transition hold (a retarget) starts its own one fresh review under its
    # turn key; a pending request is not delivered across it.
    # An App reviewer (#1163) never shows as requested: its handoff is always the
    # host's, so a head with no verdict and no run is delivered the same way.
    if ((review_kick.already_requested(loop, pr) or config.reviewer_is_app(loop))
            and c.kicks.get(str(number)) != head
            and not boundary
            and head_postdates_arming and (watch_mode.TEST or mins >= KICK_AFTER_MIN)):
        try:
            sent = review_kick.kick(loop, pr, head, author, f"sweep-review-{number}")
        except Exception as exc:
            sent = False
            log(f"#{number} @ {head[:7]}: review request not delivered: "
                f"{type(exc).__name__}: {exc}")
        if sent:
            c.kicks[str(number)] = head
            log(f"#{number} @ {head[:7]}: a pending review request had started no "
                "review — delivered it to the reviewer gate")
    if ((watch_mode.TEST or mins > c.grace["reviewer"]) and head_postdates_arming
            and not live_turn(loop, number, head, "reviewer")):
        return (f"reviewer never posted a verdict — head {head[:7]} observed "
                f"{mins / 60:.1f}h ago, 0 verdicts at this head (`hermes dk "
                f"review --loop {loop['id']} --pr {number}` asks for one)")
    return ""


def _stuck_states(c: _Sweep) -> None:
    loop, now = c.loop, c.now
    # A fresh clock (review on #937): ``now`` was taken before this sweep's GitHub reads and
    # hand-offs, so a claim made since would look dated in the future and be reported dead.
    stuck: list[tuple[str, str]] = died_locks(loop, c.st._load(c.st.locks, {}) or {}, time.time())
    queue = c.st.queue_all()
    for seat, items in (queue if isinstance(queue, dict) else {}).items():
        for key, entry in (items if isinstance(items, dict) else {}).items():
            if config.is_fixer_push_hold(entry):
                continue  # reported once per head as a stall above, not on every sweep
            at = state_mod.mark_at(entry)
            if at is None:
                continue  # unreadable: this sweep's drain drops it (#80)
            age = (now - at) / 60
            if age > config.stall_grace_s(loop, seat) / 60:
                stuck.append((f"queue:{seat}:{key}",
                              f"  {seat} queue: {key} waiting {age:.0f}m — {entry.get('reason')}"))

    # The same cooldown the stall alerts use, on a stable key per stuck mark: an unrepaired
    # mark is worth one warning per window, not the identical two lines every 15 minutes.
    c.reported = [line for key, line in stuck if c.due(key)]


def _report_stalls(c: _Sweep) -> None:
    alerts, reported, lines = c.alerts, c.reported, c.lines
    if alerts or reported:
        header = f"[{c.loop['id']}] {c.loop['repo']}"
        if alerts:
            lines.append(f"⚠️ Diaktoros {header} — {len(alerts)} silent stall(s):")
            for number, kind, title in alerts:
                lines.append(f"  #{number}  {kind}")
                lines.append(f"        {title}")
        if reported:
            lines.append(f"⚠️ Diaktoros {header} — {len(reported)} stuck state(s):")
            lines.extend(reported)
        lines.append("Pending adjudicator delivery retries on the next sweep; other stalls "
                     "need investigation. Check the gateway log before re-driving a route.")


def _save_alerts(c: _Sweep) -> None:
    # Persist the cooldown stamps the moment they are decided, before ``drain_queued`` and the
    # observer's notify/retry/flush can raise (#158(3)). Those are best-effort work; a failure in
    # any of them must cost this sweep's line, never the ``raised`` record the next sweep reads —
    # otherwise one bad flush silently re-alerts the same stall on every following sweep. The
    # health settlement below rewrites ``watch`` again and saves once more; this earlier save is
    # what survives a raise in between.
    watch = c.watch
    watch["alerts"] = {**{k: v for k, v in c.marks.items()
                          if (k in c.present or k.startswith(tuple(c.unjudged)))
                          and watch_mode.valid_clock(v, c.now) is not None},
                       **c.raised}
    watch["review_kicks"] = c.kicks
    watch["last_run"] = now_iso()
    c.st.watch_save(watch)


def _notify_stalls(c: _Sweep) -> None:
    # The observer feed, last and best effort. Only the alerts this sweep actually decided to
    # raise become notices (each stamped with the sweep's own clock, so re-raising a stall after
    # the cooldown is a new notice while a second sweep in the same breath is not), and a
    # destination that cannot be reached costs a retry, never this sweep's job.
    for number, kind, _title in c.alerts:
        head = next(((pr.get("head") or {}).get("sha", "") for pr in c.prs
                     if isinstance(pr, dict) and pr.get("number") == number), "")
        # A push-off hold is one fact per head: its notice is keyed without the sweep clock,
        # so a cooldown re-raise in the sweep output never pings the observer twice.
        identity = ("fixer-push-off" if kind.startswith(PUSH_OFF_KIND)
                    else f"{kind[:40]}#{int(c.now)}")
        observer.notify(c.loop, c.st, "stall", number, head, identity=identity,
                        outcome=kind, next_turn="you")


def _best_effort_sweeps(c: _Sweep) -> None:
    loop, st, prs, watch = c.loop, c.st, c.prs, c.watch
    try:
        ci_fix.sweep(loop, st, prs, log)      # #306: a red required check, said once per head
    except Exception as exc:
        log(f"ci sweep failed: {type(exc).__name__}: {exc}")
    try:
        stale_approval.sweep(loop, st, prs, watch, log)   # #473: approved, then CI went bad
    except Exception as exc:
        log(f"stale approval sweep failed: {type(exc).__name__}: {exc}")
    try:
        ruled_approval.sweep(loop, st, prs, log)  # #905: an ACCEPT approves, its block dismissed
    except Exception as exc:
        log(f"ruled approval sweep failed: {type(exc).__name__}: {exc}")
    try:
        main_check.sweep(loop, st, log)       # a required check red on main after merges
    except Exception as exc:
        log(f"main check failed: {type(exc).__name__}: {exc}")
    try:
        ready.sweep(loop, st, log)            # #654: one notice when the ready-to-merge set changes
    except Exception as exc:
        log(f"ready sweep failed: {type(exc).__name__}: {exc}")
    try:
        merge_check.refresh(loop, st, log=log)    # #1163: the diaktoros/review check, every head
    except Exception as exc:
        log(f"merge check failed: {type(exc).__name__}: {exc}")
    try:                                          # #1154: a closed PR's conflict record goes
        conflict_person.prune(st, [p.get("number") for p in prs if isinstance(p, dict)])
    except Exception as exc:
        log(f"conflict record prune failed: {type(exc).__name__}: {exc}")
    try:                                          # #1246: a closed PR's pending escalation goes
        for number in escalate_pending.prune(st, [p.get("number") for p in prs
                                                  if isinstance(p, dict)]):
            log(f"#{number}: pending escalation dropped (the PR is closed, or it lapsed)")
    except Exception as exc:
        log(f"pending escalation prune failed: {type(exc).__name__}: {exc}")


def _finish(c: _Sweep) -> None:
    loop, st = c.loop, c.st
    try:
        # #914: the issues a shadow review asked for, once the real review is in and did not
        # file them (off unless shadow.file_issues; bounded per day).
        for issue in shadow_issues.file_pending(loop):
            log(f"shadow-found issue #{issue} filed")
    except Exception as exc:
        log(f"shadow issue filing failed: {type(exc).__name__}: {exc}")
    if observer.retry(loop, st):
        log("observer: retried an undelivered notice")
    observer.flush(loop, st, wait_s=0 if watch_mode.TEST else observer.digest_wait(loop))
    record_overrides(loop, st)
    send_weekly_report(loop, st)

    # ``watch["alerts"]`` and ``last_run`` were saved above, before the best-effort work; here only
    # the health settlement (which rewrites ``watch``) is committed, on the same read-modify-write.
    c.finish_reads()
    st.watch_save(c.watch)
    st.note(f"run: {len(c.alerts)} alert(s), {len(c.reported)} stuck, {len(c.prs)} open PRs")


LIVE_TURN = ("pending", "claimed", "launching", "running", "waiting")


def live_turn(loop: dict, number: int, head: str, seat: str) -> bool:
    """Whether ``seat``'s turn on this PR head is queued or running in the run ledger."""
    from diaktoros.run_supervisor import views
    return views.turn_state(config.host_path("ledger"), loop["repo"], number, head, seat) in LIVE_TURN


def stall_key(number: int, head: str, kind: str) -> str:
    """The cooldown key for one stall: the PR, its head and what the stall is — with the ages
    and counts its wording carries masked, so "adjudicating for 1.2h" and "... 1.3h" on the next
    sweep are one stall, not a new one every six minutes (#77)."""
    return f"{number}:{head[:7]}:{re.sub(r'[0-9]+(?:[.][0-9]+)?', '#', kind)[:24]}"


def parked_kind(loop: dict, marker: dict, number: int, head: str,
                marker_grace: float) -> str | None:
    """The stall a breach marker at ``head`` amounts to: ``""`` when it is none yet, None when
    the marker is not this head's (#98).

    ``awaiting-adjudication`` — no ruling run has started — is parked once it is older than
    ``marker_grace_min``. ``adjudicating`` means a ruling is out: while its adjudicator run is
    live in the ledger it is not a stall at all (the run's own clocks bound it), and with no
    live run it is one only once older than ``config.adjudicating_stall_s`` — the adjudicator's
    whole worst-case turn, or ``marker_grace_min`` if longer — counted from when it started.
    """
    if not isinstance(marker, dict) or marker.get("head") != head:
        return None
    from diaktoros.run_supervisor import views
    # A shadow marker can be re-armed at its head for a later dissent (#1111): only its own
    # round's ruling ends it, not the earlier round's.
    shadow = (marker.get("escalated") or {}).get("by") == gate.SHADOW_ESCALATION
    if views.ruling_at(config.host_path("ledger"), loop["repo"], number, head,
                       turn_key=f"breach:{marker.get('rounds')}" if shadow else None):
        return ""             # ruled: the decision is the operator's, and the ruling said so
    if marker.get("status") != "adjudicating":
        mins = age_min(marker.get("at"))
        if past(mins, marker_grace):
            return (f"parked awaiting adjudication {lasting(mins)} "
                    f"(marker {marker.get('at') or 'unknown'})")
        return ""
    run = views.turn_state(config.host_path("ledger"), loop["repo"],
                           number, head, "adjudicator")
    if run in LIVE_TURN:
        return ""
    since = marker.get("adjudicating_at") or marker.get("at")
    mins = age_min(since)
    if past(mins, 0.0 if watch_mode.TEST else config.adjudicating_stall_s(loop) / 60):
        return (f"adjudicating {lasting(mins)} (since {since or 'unknown'}) but no "
                f"adjudicator run is live ({run or 'no run on record'}) — the ruling is not "
                "coming by itself")
    return ""


def past(mins: float | None, grace: float) -> bool:
    """Whether an age is past ``grace``. An unreadable time (None) is past every grace (#563):
    a bad clock must raise the stall, never hide it as "just now"."""
    return mins is None or mins > grace


def ago(mins: float | None) -> str:
    return "at an unreadable time" if mins is None else f"{mins / 60:.1f}h ago"


def lasting(mins: float | None) -> str:
    return "since an unreadable time" if mins is None else f"for {mins / 60:.1f}h"


def died_locks(loop: dict, locks: dict, now: float) -> list[tuple[str, str]]:
    """Seat claims old enough to call their run dead: past twice the seat-lock TTL, which is
    ``ttl_min`` raised to the loop's whole worst-case turn (``config.seat_ttl_s``, #98) — so a
    healthy turn with a long budget is never reported as one that died.

    Each line carries a stable key, so the sweep's cooldown warns once per window instead of on
    every 15-minute sweep, and the wording names the prune rather than a slot that frees itself:
    ``drain()`` drops the expired mark at the top of the next sweep (#77).
    """
    lines: list[tuple[str, str]] = []
    for seat, entries in (locks if isinstance(locks, dict) else {}).items():
        for key, entry in (entries if isinstance(entries, dict) else {}).items():
            at = state_mod.mark_at(entry)
            if at is None:
                continue  # unreadable: not a run that died, and the next ``active`` prunes it (#80)
            age = now - at
            # On the budget the claim was taken with, if longer than the loop's now (#98).
            # This seat's own turn: another seat's longer budget never keeps it alive (#98).
            recorded = config.claim_budget(entry)   # None: a legacy claim gets its seat's own
            if age < 0:
                # Dated ahead of the clock: ``state._mark_live`` treats it as expired, so report
                # it as a dead claim too — a bad clock must raise the stall, not hide it (#794).
                lines.append((f"lock:{seat}:{key}",
                              f"  {seat} slot claim on {key} is dated {-age / 60:.0f}m in the "
                              f"future — treated as dead; the mark is pruned on the next sweep"))
            elif age > config.seat_died_after_s(loop, seat=seat, recorded=recorded):
                lines.append((f"lock:{seat}:{key}",
                              f"  {seat} slot held {age / 60:.0f}m on {key} — that run died; the "
                              f"mark is pruned on the next sweep"))
    return lines


def retire_stale_runs(loop: dict, prs: list, lines: list[str]) -> None:
    """With a complete open-PR listing, cancel waiting runs of PRs that closed or whose head
    moved, so a hold on them does not outlive the PR in status (#629)."""
    from diaktoros.run_supervisor import Supervisor, presence_marker
    ledger = config.host_path("ledger")
    if not ledger.exists():
        return
    heads = {pr.get("number"): (pr.get("head") or {}).get("sha")
             for pr in prs if isinstance(pr, dict)}
    try:
        Supervisor(ledger, presence=presence_marker()).retire_stale_waiting(loop["repo"], heads)
    except Exception as exc:
        lines.append(f"⚠️ Diaktoros [{loop['id']}] stale held runs not retired: "
                     f"{type(exc).__name__}: {exc}")


def sweep_ledger(ledger: pathlib.Path, presence: pathlib.Path | None = None) -> list[str]:
    """Deliver the run ledger's operator notices; return lines for a failed sweep.

    A ledger that vanished since the host last opened it — its ``.present`` marker remains, or
    ``presence`` (the loop config dir's marker, which survives a wiped state dir) names it — is
    recreated here, which records a one-time notice that this sweep then delivers. Where no
    ledger ever existed nothing is created.
    """
    from diaktoros.run_supervisor import Supervisor, _names, ledger_marker
    if not ledger.exists() and not ledger_marker(ledger).exists() and not _names(presence, ledger):
        return []
    try:
        sup = Supervisor(ledger, presence=presence)
        sup.recover()  # no runtime configured here: never launch a worker
        sup.notify(lambda message: print(message, flush=True))
    except Exception as exc:
        return [f"⚠️ Diaktoros operator notification sweep failed: {type(exc).__name__}: {exc}"]
    return []


def main() -> None:
    ap = argparse.ArgumentParser(description="Diaktoros stall watchdog for configured loops")
    ap.add_argument("--loop", help="loop id (default: every configured loop)")
    ap.add_argument("--drain", action="store_true", help="start a queued run and print nothing else")
    ap.add_argument("--seat", default="reviewer", choices=["reviewer", "fixer"])
    ap.add_argument("--conflicts", action="store_true",
                    help="wait for a requested conflict scan (after a merge) and run only that")
    args = ap.parse_args()
    global EMIT
    EMIT = lambda line: print(line, flush=True)  # noqa: E731
    budget = watchdog_budget()
    gh.begin_gate(time.monotonic() + budget, per_call=min(WATCHDOG_PER_CALL_S, budget))
    try:
        run(args, budget)
    except gh.GateBudgetExceeded as exc:          # e.g. the drain path's hook read
        print(watch_health.budget_spent_line(f"[{args.loop or 'all loops'}]", budget, exc))
    finally:
        gh.end_gate()


def run(args: argparse.Namespace, budget: float) -> None:
    # A migration holds the install (#431): sweeping, draining or healing now would act under
    # names being moved. Say so once per run and do nothing else; the next run after it ends
    # re-drives what the gates deferred meanwhile.
    from diaktoros import migrate
    held = migrate.migrating()
    if held is not None:
        if not args.drain:
            print(f"⏸️ Diaktoros: paused — {migrate.describe(held)}", flush=True)
        return
    # The cron job runs this with no --loop: one loop file the loader refuses (for any repo)
    # is reported by name, like any other per-loop failure, and every other loop still sweeps.
    refused: list[tuple[str, str]] = []
    if args.loop:
        try:
            loops = [config.load_id(args.loop)]
        except config.ConfigError as exc:
            # A scripted drain or cron call with a bad --loop must not look successful.
            if (config.config_dir() / f"{args.loop}.json").exists():
                print(f"⚠️ Diaktoros [{args.loop}] watchdog failed: ConfigError: {exc}")
            else:
                print(f"no loop config named {args.loop}")
            sys.exit(1)
    else:
        loops, refused = config.readable_loops()

    if args.conflicts:
        from diaktoros import conflict_scan
        for loop in loops:
            st = state_mod.state_for(loop)
            # Inside this process's GitHub budget (main's begin_gate), with half left for a scan.
            conflict_scan.run(st, lambda: watch_conflicts.scan_conflicts(loop, st),
                              lifetime=budget / 2)
        return

    if args.drain:
        for loop_id, reason in refused:
            # Drain prints nothing else, but a refused file must not go quiet: stderr.
            print(f"⚠️ Diaktoros [{loop_id}] not drained: ConfigError: {reason}", file=sys.stderr)
        for loop in loops:
            st = state_mod.state_for(loop)
            armed, armed_error = (True, "") if watch_mode.TEST else gate.hooks_read(loop)
            if not armed:
                if args.loop:
                    print(f"{loop['id']}: hooks are paused — nothing drained" if armed is False
                          else f"{loop['id']}: hook list unreadable ({armed_error}) — nothing drained")
                continue
            try:
                fired = drain(loop, st, args.seat)
            except gh.GateBudgetExceeded as exc:
                print(watch_health.budget_spent_line(f"[{loop['id']}]", budget, exc))
                return
            if not fired and not st.queue_items(args.seat) and args.loop:
                print(f"{loop['id']}: {args.seat} queue empty")
        return

    out: list[str] = []
    # The supervisor outbox is independent of GitHub listing availability or
    # paused hooks. It uses the existing cron stdout delivery path.
    from diaktoros.run_supervisor import presence_marker
    out.extend(sweep_ledger(config.host_path("ledger"),
                            presence=presence_marker()))
    # Failures no loop could be named for (a malformed payload, a broken config) — swept by
    # every run, scoped or not: an install whose cron jobs are all ``--loop``-scoped would
    # otherwise never say them. The ledger's claims keep overlapping sweeps to one alert.
    out.extend(sweep_gate_failures(gate_failures.fallback_ledger(), "(no loop)",
                                   0.0 if watch_mode.TEST else 6 * 3600, may_redrive=True))
    for loop_id, reason in refused:
        out.append(f"⚠️ Diaktoros [{loop_id}] watchdog failed: ConfigError: {reason}")
    for loop in loops:
        lines: list[str] = []
        try:
            out.extend(sweep_loop(loop, state_mod.state_for(loop), lines))
        except gh.GateBudgetExceeded as exc:      # GitHub is hanging: every loop would too
            out.extend(lines)
            try:
                out.extend(watch_health.sweep_budget_spent(loop, state_mod.state_for(loop),
                                                           budget, exc))
            except Exception as err:              # never hide the stop itself
                out.append(watch_health.budget_spent_line(f"[{loop.get('id', '?')}]", budget, exc)
                           + f" (could not record it: {type(err).__name__})")
            break
        except Exception as exc:                  # one bad loop must not hide the others
            out.extend(lines)
            out.append(f"⚠️ Diaktoros [{loop.get('id', '?')}] watchdog failed: "
                       f"{type(exc).__name__}: {exc}")
    if out:
        print("\n".join(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                      # never crash the scheduler silently
        print(f"⚠️ Diaktoros watchdog failed: {type(exc).__name__}: {exc}")
        sys.exit(0)
