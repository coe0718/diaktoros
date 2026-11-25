"""A pending run's claim and a claimed run's production turn (#758), moved out of the core.

``ClaimAndLaunch`` is a mixin: ``Supervisor`` inherits it, so ``Supervisor(db)._claim()`` and a
test's ``patch.object(Supervisor, ...)`` work as before, with one binding. The steps are the
claim reads (``_claim_read_*``), ``_claim_commit``, ``_production_turn`` (its pacing and CI
holds are the ``turn_holds.TurnHolds`` mixin, which this one inherits), the ``_prepare_*_launch``
steps, ``_budget_kill_error`` and ``_production_finish`` (the shadow's own
steps are the ``shadow_turns.ShadowTurns`` mixin); their state is ``_ClaimRead``, ``_Turn`` and
``_Launch``, defined here. The mixin uses ``_connect()`` and the rest of ``Supervisor``, and the
package's names through the package (``run_supervisor.X``), so a test patching
``run_supervisor.X`` still reaches this code.
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import NamedTuple

from .. import claimant, run_supervisor, runtime_detect, util
from ..state import ledger
from . import claims, merge_hold, pr_facts, turn_prompts, waits
from .turn_holds import TurnHolds


def _native_unverified(error):
    """Cleanup or broker wrappers must not erase missing native exit proof."""
    from ..native_lifecycle import CleanupIncomplete
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, CleanupIncomplete):
            return True
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)
    return False


@dataclass
class _ClaimRead:
    """What a pending row's claim-time reads found; ``_claim_commit`` settles the row from it.
    ``retry_read`` is a legitimate wait (a draft), left pending. ``read_error`` is a read that
    failed: counted and backed off like a failed turn, then failed with its reason, so it can
    never sit pending and invisible (#53, review on #97). ``refused`` is a row that can never
    publish; ``unavailable``, one whose review generation cannot be pinned. ``fence`` is the PR's
    merge hold as it stood before the reads (``merge_hold.fences``, #2468); None checks only a
    hold live at the commit."""
    generation: str | None = None
    unavailable: str | None = None
    retry_read: bool = False
    read_error: str = ""
    superseded: str | None = None
    refused: str = ""
    fence: tuple | None = None
    since: float = 0.0          # when the claim's reads began (#2608: its own limited answers)


@dataclass
class _Turn:
    """One production turn's state, shared by its steps and by ``_production_finish``, which
    runs however the turn ends."""
    run_id: str
    owner: str
    budget: int
    rc: int | None = None
    error: str | None = None
    retry: bool = False
    stopped: bool = True
    observed: dict = field(default_factory=dict)
    breach: tuple | None = None
    claim: object = None
    paced_until: float | None = None
    cancel: bool = False       # ended before launch by a fact, not a fault: cancelled (#723)
    row: sqlite3.Row | None = None
    loop: dict | None = None
    launched: bool = False     # its merge check says it is running (#753)
    recorded: bool | None = None   # a shadow's submission written (#1361); None: not tried yet


class _Launch(NamedTuple):
    """What a turn is launched with: its prompt, its broker scope, the change it reviews or
    fixes, and a conflict turn's merge."""
    prompt: str
    scope: object
    change: object = None
    merged: dict | None = None


CI_HOLD_READS = "ci-hold-reads.json"     # {run id: when the light poll last read the hold}
SHADOW_AGREES = 'adjudication no longer current: the shadow review agrees now'   # #2688


def _ci_poll_hold_due(row, st, now: float | None = None) -> bool:
    """Whether the CI light poll reads the maintainer hold live this time (#1708): once the row
    has been queued ``run_supervisor.CI_HOLD_READ_S``, then once per that period, timed by the
    loop's ``ci-hold-reads.json`` (the poll's own record: the ledger's ``updated`` is reset
    each time the worker re-queues the row). A due read is recorded here; records older than
    a day are dropped, so the file stays as small as the set of rows held for CI."""
    now = time.time() if now is None else now
    path = st.dir / CI_HOLD_READS
    raw = st.kv_get(path, {})
    reads = {k: float(v) for k, v in (raw.items() if isinstance(raw, dict) else ())
             if isinstance(v, (int, float)) and now - float(v) < 86400}
    last = max(reads.get(row['id'], 0.0), float(row['created'] or 0))
    if now - last < run_supervisor.CI_HOLD_READ_S:
        return False
    reads[row['id']] = now
    st.kv_set(path, reads)
    return True


def _ci_poll_live_hold(row, loop: dict) -> bool:
    """The live maintainer-hold read (``pr_hold.read``, two GitHub calls) for the CI light poll:
    True, sending the row to the full claim, when the PR is held or the read failed (an
    unreadable hold is not "not held"); the full claim then parks it or backs it off."""
    from .. import pr_hold
    hold, error = pr_hold.read(loop, row['pr'])
    return bool(hold or error)


def ci_wait_expired(row) -> bool:
    """Past ``run_supervisor.CI_WAIT_MAX_S`` from when the run was queued (#241): only a red
    hold remains. The turn's hold and the claim's light poll both ask it here."""
    return time.time() - row['created'] >= run_supervisor.CI_WAIT_MAX_S


def _ci_hold_text(head: str, checks, fixing: bool, red: bool, review_only: bool) -> str:
    """The hold's one line, from the gated ``checks`` that decided it."""
    from .. import ci
    return run_supervisor.ci_hold_line(head, (
               f"{len(checks.cancelled)} check(s) cancelled, re-run them on "
               f"GitHub ({ci._names(checks.cancelled[:5])})"
               if checks.cancelled and not red else
               f"{len(checks.failed)} {run_supervisor.CI_FIXER_FIRST}"
               if fixing else
               f"{len(checks.failed)} {run_supervisor.CI_FAILED_WAIT} "
               f"({ci._names(checks.failed[:5])}); "
               + ("no fixer takes a review-only PR" if review_only else
                  "the fixer's CI fixes are done")
               if red else
               f"{len(checks.missing)} required check(s) never reported "
               f"({ci._names(checks.missing[:5])}); check the name"
               if checks.missing else
               f"{len(checks.pending)} check(s) still running or not reported"))


def _nudged(row, prompt: str) -> str:
    """The turn's prompt, with the publish nudge when an earlier turn at this row ended without
    publishing: placed before "This round", not first, so a retry's prompt keeps the same stable
    prefix (#1649)."""
    from .. import prompts
    if not run_supervisor.unpublished_before(row):
        return prompt
    return prompts.before_this_round(prompt, run_supervisor.PUBLISH_NUDGE)


def _turn_role(row) -> str:
    """The prompt a ledger row's turn runs under, for its recorded prompt revision: a fixer row
    may be a conflict or a CI-fix turn, and a shadow turn is told what the reviewer is (#218)."""
    from .. import ci_fix
    if row['seat'] == 'fixer' and str(row['turn_key'] or '').startswith(run_supervisor.CONFLICT_KEY):
        return 'conflict'
    if row['seat'] == 'fixer' and ci_fix.is_ci_fix(row['turn_key']):
        return 'ci_fix'
    return 'reviewer' if row['seat'] == run_supervisor.SHADOW else row['seat']


def _turn_pool(loop: dict, row, review_only: bool) -> tuple[str, int | None]:
    """The pool a turn is counted in (#772) and that pool's daily cap. The count at launch
    re-checks the cap in the same locked step (#1378): admission came before staging, and a
    turn admitted alongside this one may have taken the last slot meanwhile."""
    from .. import config, pacing
    if review_only:
        return pacing.REVIEW_ONLY_SEAT, config.review_only_daily(loop)
    return row['seat'], config.seat_daily_turns(loop, row['seat'])


def _cap_held(turn: _Turn, seat: str, cap: int) -> None:
    """Hold ``turn`` until midnight: the daily turn pool ``seat`` counts in is at ``cap``. The
    admission check (``_pacing_hold``) and the count at launch (#1378) both end here."""
    from .. import pacing
    turn.paced_until = pacing.next_midnight()
    label = run_supervisor.REVIEW_ONLY_LABEL if seat == pacing.REVIEW_ONLY_SEAT else seat
    turn.error = run_supervisor.daily_hold_line(label, run_supervisor.DAILY_TURN_CAP, cap,
                                                pacing.when(turn.paced_until))


def _provider_strike(turn: _Turn, account: str) -> float | None:
    """#1155: a failed turn that the provider killed (an HTTP error or an unreachable upstream,
    as the inference proxy recorded it) counts toward ``provider_breaker_failures`` in a row on
    its account, which then holds the account as an outage does (#1325). Any other failure of a
    turn that ran ends the run of them; one that never launched (it raised before this) leaves
    it. Returns when the hold ends, else None."""
    from .. import config, pacing
    limit = config.provider_breaker_failures(turn.loop)
    error = turn.observed.get('provider_error')
    if not limit:
        return None
    if not pacing.provider_failure(error):
        pacing.reset_provider_failures(account)
        return None
    return pacing.count_provider_failure(account, str(error), limit)


def _conflict_waits(loop: dict, row) -> str:
    """Why a conflict row waits for the fixer (#1907), or ``""``: ``watch_conflicts.fix_owed``,
    read again at the claim and at the launch, since a verdict can land after the row was queued.
    A row that waits is superseded once the fixer pushes; the next sweep merges into that head."""
    from .. import state as state_mod, watch_conflicts
    owed = watch_conflicts.fix_owed(loop, state_mod.state_for(loop), row['pr'], row['head'])
    if owed:
        util.log(f"#{row['pr']} @ {str(row['head'])[:7]} conflict turn waits: {owed}")
    return owed


def _before_shadow_queued(review, created) -> bool:
    """Whether a review was posted before the shadow row was queued at ``created`` (#1684).

    GitHub's ``submitted_at`` has whole seconds while ``created`` has fractions, so a review
    posted later in the second the shadow was queued reads as earlier: that whole second is
    excluded (kept only if strictly before ``floor(created)``). An unreadable stamp fails closed:
    a review the shadow can't place in time may be the real verdict it must not see."""
    import math
    at = util.iso_epoch(review.get('submitted_at') if isinstance(review, dict) else None)
    return at is not None and at < math.floor(float(created))


def _limit_wait_began(error) -> float | None:
    """When a row's rate-limit wait began, from its recorded reason (#2608), or None."""
    text = str(error or '')
    marker = f'{run_supervisor.LIMIT_SINCE} '
    if marker not in text:
        return None
    rest = text.split(marker, 1)[1].split()
    return util.iso_epoch(rest[0].rstrip(';')) if rest else None


def _limit_note(until, started: float, reset: float) -> str:
    """A rate-limited claim read's note (#2608): GitHub's reset; when the wait's bound comes
    first, that the read is at the bound, before the reset (#2613 review, F7); and since when the
    limit has been waited for. With no read due for the limit (``until`` None: past the bound, or
    an ordinary failure between limited ones) only that start, so the bound holds."""
    def hm(at):
        return time.strftime('%H:%MZ', time.gmtime(at))
    since = (f'{run_supervisor.LIMIT_SINCE} '
             f'{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))}')
    if until is None:
        return since
    note = f'{run_supervisor.LIMIT_WAIT} at {hm(reset)}'
    if until < reset:     # the bound, before the reset (one inside the margin is after it, F7)
        note += f', read again at {hm(until)} ({run_supervisor.LIMIT_BOUND})'
    return f'{note}; {since}'


def _claim_read_reason(read_error: str) -> str:
    """A failed claim-time read's recorded reason, naming the claimant (#1337): a failure from a
    copy of the plugin older than the install names the process that made it."""
    return f'{run_supervisor.CLAIM_READ_FAILED}: {read_error} [{claimant.identity()}]'


class ClaimAndLaunch(TurnHolds):
    """The claim and turn methods of :class:`~diaktoros.run_supervisor.Supervisor`."""

    def _claim(self):
        # A migration holds the install (#431): no run starts under names being moved. The
        # pending rows wait where they are; the worker that finds nothing here exits as usual.
        from ..state import migrate
        if migrate.migrating() is not None:
            return None
        # Code older than the install never claims (#1337): a process that loaded the plugin
        # before a reinstall leaves the pending rows to a worker on the installed code.
        if self.production_config and (why := claimant.stale()):
            util.log(f"claim refused: {why} ({claimant.identity()})")
            return None
        # Snapshot candidates without taking a writer lock. A slow GitHub read
        # must not block unrelated enqueues, heartbeats, or receipt commits.
        with self._connect() as con:
            candidates = con.execute("SELECT * FROM runs WHERE state='pending' "
                                     "ORDER BY created,id").fetchall()
            fences = merge_hold.fences(con, time.time())    # before any read (#2468)
        for row in claims.claim_order(candidates):
            if (self.production_config and row['seat'] in self.capacity
                    and self._unstartable(row)):
                continue        # #2609: no GitHub read for a row that cannot start now
            if (self.production_config and row['seat'] in self.capacity
                    and self._ci_poll(row)):
                waits.note(self, row['id'])     # why it waits, once per reason (#2080)
                continue        # still held for CI: the light poll re-timed it (#1648)
            read = _ClaimRead(fence=fences.get((row['repo'], row['pr']), (0, False)),
                              since=time.time())
            if self.production_config and row['seat'] == 'reviewer':
                self._claim_read_reviewer(row, read)
            if self.production_config and row['seat'] == run_supervisor.SHADOW:
                self._claim_read_shadow(row, read)
            if row['seat'] not in self.capacity:
                continue  # a worker spawned with another seat set never claims this row
            if self.production_config:
                self._claim_read_live(row, read)
            claimed = self._claim_commit(row, read)
            if claimed is not None:
                return claimed
            waits.note(self, row['id'])         # a claim read that failed backs off (#2080)
        return None

    def _claim_read_reviewer(self, row, read: _ClaimRead) -> None:
        """A reviewer row's claim-time read: the PR's state and head, its review generation,
        and whether a review at this head already answers the turn."""
        from .. import config, gh
        from ..review_receipt import ReceiptDenied, generation_for
        try:
            loop = config.by_repo(row['repo'])
            if loop is None:
                read.unavailable = 'loop not configured'
                return
            pr = gh.api(loop, f"/repos/{row['repo']}/pulls/{row['pr']}",
                        login=loop['read_token'])
            # gh.api answers None for any failed read (a 502 included): that is a
            # read to retry, never a verdict on the PR (#53). A closed PR or a moved
            # head retires this turn (a reopen/redelivery re-arms it); a draft waits
            # for ready, as the fixer's claim does.
            if not isinstance(pr, dict) or pr.get('number') != row['pr']:
                read.read_error = 'PR unreadable (GitHub read failed)'
            elif pr.get('state') == 'closed':
                read.superseded = 'PR closed before the review started'
            elif (pr.get('head') or {}).get('sha') != row['head']:
                read.superseded = run_supervisor.REVIEW_HEAD_MOVED
            elif pr.get('state') != 'open' or pr.get('draft') is not False:
                read.retry_read = True
            else:
                read.generation = generation_for(pr, loop, row['pr'], row['head'])
                # A review at this exact head already answers the turn (e.g. one
                # posted in the GitHub UI): a second would spend another round.
                # The same-head retarget's fresh-review turn exists to review
                # again at this head, so it is exempt.
                # A dispute re-review (#894) exists to review again at this head, too, and so
                # does a maintainer's reconsider (#1334).
                if not str(row['turn_key'] or '').startswith(('retarget:', run_supervisor.DISPUTE_KEY,
                                                             run_supervisor.RECONSIDER_KEY)):
                    from .. import gate
                    reviews = pr_facts.effective_reviews(loop, row, gh.reviews(loop, row['pr']),
                                                         self.db)
                    if not isinstance(reviews, list):
                        read.read_error = 'reviews or receipts unreadable (GitHub read failed)'
                    else:
                        answered = [r for r in gate.reviews_at_head(reviews, loop, row['head'])
                                    if gh.review_state(r) in ('APPROVED', 'CHANGES_REQUESTED')]
                        if answered:
                            who = gate.reviewer_login(answered[-1]) or 'a reviewer'
                            read.superseded = f'superseded: reviewed at this head by {who}'
        except ReceiptDenied as exc:
            read.unavailable = f'review generation unavailable: {exc}'
        except Exception as exc:
            read.read_error = f'{type(exc).__name__}: {exc}'[:200]

    def _claim_read_live(self, row, read: _ClaimRead) -> None:
        """The production claim's live reads, in order: an escalated head, a maintainer's
        hold, the adjudicator's facts, the fixer's verdict. Each may add to ``read``."""
        if row['seat'] in ('reviewer', 'fixer') and not read.superseded:
            self._claim_read_escalated(row, read)
        if row['seat'] in claims.PR_HOLD_SEATS and not read.superseded:
            # A maintainer holds this PR (#477): the row waits, pending, like a draft's, and
            # starts after release. An unreadable hold is not "not held": it waits too.
            from .. import config, pr_hold
            try:
                hold_loop = config.by_repo(row['repo'])
                if hold_loop is not None:
                    hold, hold_error = pr_hold.read(hold_loop, row['pr'])
                    read.retry_read = read.retry_read or bool(hold)
                    read.read_error = read.read_error or hold_error
            except Exception as exc:
                read.read_error = read.read_error or f'{type(exc).__name__}: {exc}'[:200]
        if row['seat'] == 'adjudicator':
            self._claim_read_adjudicator(row, read)
        if row['seat'] == 'fixer':
            self._claim_read_fixer(row, read)
        if row['seat'] == 'issue_fixer' and not read.superseded:
            # A prerequisite declared after the row was queued (#775): it waits, pending, and
            # starts once the dependency is merged or waived. Unreadable holds too.
            from .. import config, issue_deps
            try:
                dep_loop = config.by_repo(row['repo'])
                if dep_loop is not None and issue_deps.hold_reason(dep_loop, row['pr']):
                    read.retry_read = True
            except Exception:
                read.retry_read = True

    def _claim_read_escalated(self, row, read: _ClaimRead) -> None:
        """The operator escalated this head (#459): it waits for a ruling, so a review or fix
        queued for it is retired (``read.superseded``), as a spent cap would have stopped it.
        A local read (the breach marker). The full claim asks it, and so does the CI light
        poll (``_ci_poll``, #1708), which sends an escalated row to the full claim."""
        from .. import config, gate, state as state_mod
        try:
            escalated_loop = config.by_repo(row['repo'])
            marker = (state_mod.state_for(escalated_loop).breach_get(row['pr'])
                      if escalated_loop is not None else None)
            # A head the sweep escalated because of a dispute (#894) still takes the
            # maintainer's requested re-review: an approval then supersedes the ruling.
            rereview = (str(row['turn_key'] or '').startswith(run_supervisor.DISPUTE_KEY)
                        and ((marker or {}).get('escalated') or {}).get('by') == 'dispute')
            # Nor is the fix of a shadow review the adjudicator upheld (#1004 part 3): that
            # ruling is why it exists, and its marker stays at the head. Whether the ruling
            # still stands is _claim_read_fixer's (upheld_shadow_ruling).
            upheld = (row['seat'] == 'fixer'
                      and str(row['turn_key'] or '').startswith(run_supervisor.SHADOW_FIX_KEY)
                      and ((marker or {}).get('escalated') or {}).get('by')
                      == gate.SHADOW_ESCALATION)
            if (escalated_loop is not None and not rereview and not upheld
                    and gate.escalated(marker, row['head'])):
                read.superseded = 'escalated to adjudication at this head'
        except Exception as exc:
            read.read_error = read.read_error or f'{type(exc).__name__}: {exc}'[:200]

    def _claim_read_adjudicator(self, row, read: _ClaimRead) -> None:
        from .. import config
        try:
            status, _ = pr_facts.adjudication_state(config.by_repo(row['repo']), row, self.db)
            read.superseded = 'adjudication superseded' if status == 'superseded' else None
            if status == 'retry':
                read.read_error = 'adjudication facts unreadable (GitHub read failed)'
            read.retry_read = read.retry_read or status == 'wait'   # keep a hold's wait (#728)
        except Exception as exc:
            read.read_error = f'{type(exc).__name__}: {exc}'[:200]

    def _claim_read_fixer(self, row, read: _ClaimRead) -> None:
        """A fixer row's claim-time read: its push admission, the PR's state and head, and
        whether the latest effective verdict at this head still asks for changes."""
        from .. import ci_fix, config, gh, gate
        try:
            loop = config.by_repo(row['repo'])
            if loop is None:
                read.read_error = 'loop not configured'
                return
            if row['push_admitted'] != 1:
                read.refused = run_supervisor.FIXER_NOT_ADMITTED
                return
            if not config.unattended_fixer_push_enabled(loop):
                read.refused = run_supervisor.FIXER_PUSH_REVOKED
                return
            pr = gh.api(loop, f"/repos/{row['repo']}/pulls/{row['pr']}",
                        login=loop['read_token'])
            if not isinstance(pr, dict) or not isinstance(pr.get('head'), dict):
                read.read_error = 'PR unreadable (GitHub read failed)'
            elif pr['head'].get('sha') != row['head'] or pr.get('state') == 'closed':
                read.superseded = 'fixer verdict superseded'
            elif pr.get('state') != 'open' or pr.get('draft') is not False:
                read.retry_read = True
            elif str(row['turn_key'] or '').startswith(run_supervisor.SHADOW_FIX_KEY):
                # #1004 part 3: no reviewer verdict either (the head is approved); the
                # adjudicator's upholding ruling is its work order, and it must still stand.
                ruled = gate.shadows.upheld_shadow_ruling(loop, row['pr'], row['head'], db=self.db)
                if ruled is None or row['turn_key'] != f"{run_supervisor.SHADOW_FIX_KEY}{int(ruled)}":
                    read.superseded = 'upheld shadow ruling no longer current'
            elif str(row['turn_key'] or '').startswith(run_supervisor.CONFLICT_KEY):
                # #303: a conflict turn answers no verdict (the PR may be approved, or
                # unreviewed); the worker checks the merge itself before launch. #1907: but a
                # change request at the head is the fixer's first: the row waits for it.
                read.retry_read = read.retry_read or bool(_conflict_waits(loop, row))
            elif ci_fix.is_ci_fix(row['turn_key']):
                # #306: nor does a CI-fix turn; the worker re-reads CI before launch.
                pass
            else:
                reviews = pr_facts.effective_reviews(loop, row, gh.reviews(loop, row['pr']),
                                                     self.db)
                if not isinstance(reviews, list):
                    read.read_error = 'reviews or receipts unreadable (GitHub read failed)'
                else:
                    latest = gate.latest_effective_review_at_head(reviews, loop, row['head'])
                    if latest is None:
                        # Read fine, and no effective verdict yet (a receipt still
                        # landing after a retarget): a wait, like a draft.
                        read.retry_read = True
                    elif gh.review_state(latest) != 'CHANGES_REQUESTED':
                        read.superseded = 'fixer verdict superseded'
        except Exception as exc:
            read.read_error = f'{type(exc).__name__}: {exc}'[:200]

    def _claim_read_failed(self, con, row, current, error: str, now: float,
                           since: float = 0.0) -> None:
        """Settle a row whose claim-time read failed, and commit. Bounded like a failed turn
        (#53): back off, then fail with the reason and a notice. Pre-write, so `retry` (or a
        new event) re-arms it. A read the reader's spent rate limit refused waits for the reset
        instead, spending no attempt (#2608, ``_limit_wait``)."""
        self.held_for_ci = True     # this worker lingers to schedule the retry (#1302)
        until, started, reset = self._limit_wait(row, current, now, since)
        # The limit's note leads the reason (#2613 review, F8): every bounded output of it (the
        # wait line, ready's turn note) keeps it, and its start rides on, so the bound holds.
        reason = _claim_read_reason(error if started is None
                                    else f'{_limit_note(until, started, reset)} — {error}')
        if until is not None:
            # #2608: the read App ran out of calls. Not a fault of this run: it waits for the
            # reset, spending no attempt (a 35-minute outage outlasted the whole backoff).
            con.execute("UPDATE runs SET state='waiting', retry_at=?, error=?, updated=? "
                        "WHERE id=?", (until, reason, now, row['id']))
            con.execute('COMMIT')
            self._refresh_check(None, row, 'waiting')
            return
        retries = (current['retries'] or 0) + 1
        if retries < run_supervisor.MAX_RETRIES:
            con.execute("UPDATE runs SET state='waiting', retries=?, retry_at=?, "
                        "error=?, updated=? WHERE id=?",
                        (retries, now + run_supervisor.backoff(retries), reason, now, row['id']))
        else:
            con.execute("UPDATE runs SET state='failed', retries=?, retry_at=NULL, "
                        "error=?, updated=? WHERE id=?",
                        (retries, f'retry limit ({retries} attempts): {reason}'[:600],
                         now, row['id']))
        con.execute('COMMIT')
        # #1786: a read that failed for good ends the shadow; a backed-off one is still open.
        self._refresh_check(None, row, 'waiting' if retries < run_supervisor.MAX_RETRIES
                                   else 'failed')

    @staticmethod
    def _limit_wait(row, current, now: float, since: float) -> tuple:
        """``(when to read again, when the limit wait began, the reset)`` for a failed claim read that the
        reader's spent rate limit explains (``gh_calls.limited_until``: a limited answer in this
        claim, or the reader's recorded headers); when it doesn't, only the start of an earlier
        limit wait, which the counted read keeps. The wait
        began at the first limited failure, carried in the row's reason. Past ``LIMIT_WAIT_MAX``
        from then the read is ``(None, began, reset)``: it counts as any other failed read, so a limit
        that never comes back still ends through the backoff and retry limit. The read again is
        never later than that bound, however far the reset."""
        from .. import config, gh_calls
        loop = config.by_repo(row['repo'])
        login = str((loop or {}).get('read_token') or '')
        reset = gh_calls.limited_until(login, since, now) if login else None
        began = _limit_wait_began(current['error'])
        if reset is None:
            # An ordinary failure between limited ones keeps the wait's start (#2613 review,
            # F9): otherwise the next limit would start a fresh bound.
            return None, began, None
        started = began or now
        if now - started >= run_supervisor.LIMIT_WAIT_MAX:
            return None, started, reset
        return min(reset + run_supervisor.LIMIT_MARGIN_S,
                   started + run_supervisor.LIMIT_WAIT_MAX), started, reset

    def _blocked(self, con, row) -> bool:
        """Whether ``row`` cannot start now: its PR is occupied by another non-shadow turn, or
        its seat is at capacity in its repo. A shadow turn (#218) neither occupies the PR nor
        waits for it: it writes nothing, so the real reviewer and fixer never queue behind it,
        and it never queues behind them. Capacity is per repo and seat: another repo's rows
        never consume it."""
        occupied = row['seat'] != run_supervisor.SHADOW and con.execute(
            "SELECT 1 FROM runs WHERE repo=? AND pr=? AND " + run_supervisor.NOT_SHADOW + " "
            "AND (state IN ('claimed','launching','running','uncertain') "
            "OR push_intent IS NOT NULL)", (row['repo'], row['pr'])).fetchone()
        used = con.execute("SELECT COUNT(*) FROM runs WHERE repo=? AND seat=? AND "
                           "state IN ('claimed','launching','running','uncertain')",
                           (row['repo'], row['seat'])).fetchone()[0]
        return bool(occupied) or used >= self._capacity_for(row['repo'], row['seat'])

    def _unstartable(self, row) -> bool:
        """Whether ``row`` cannot start now (``_blocked``, read without the writer lock). Such a
        row costs no GitHub read (#2609): it stays pending for the worker a freed slot starts,
        and the commit's locked check still decides."""
        with self._connect() as con:
            return self._blocked(con, row)

    def _claim_commit(self, row, read: _ClaimRead):
        """Settle one candidate under the writer lock from what its read found: claim it
        (returning ``(id, owner)``), retire it, back it off, or leave it pending (None)."""
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute("SELECT * FROM runs WHERE id=?", (row['id'],)).fetchone()
            # Another claimant, recovery, or a changed generation invalidates
            # the read; never bind a resolved receipt to different row terms.
            if current is None or dict(current) != dict(row):
                con.execute("COMMIT")
                return None
            if read.refused:
                # Not a capacity question: this row can never publish, so it never waits.
                con.execute("UPDATE runs SET state='cancelled', error=?,updated=? WHERE id=?",
                            (read.refused, time.time(), row['id']))
                con.execute('COMMIT')
                self._refresh_check(None, row, 'cancelled')     # #2326
                return None
            # Recheck both capacity and PR occupancy under the writer lock (``_blocked``).
            if self._blocked(con, row):
                con.execute("COMMIT")
                return None
            now = time.time()
            if read.superseded:
                con.execute("UPDATE runs SET state='cancelled', error=?,updated=? WHERE id=?",
                            (read.superseded, now, row['id']))
                con.execute('COMMIT')
                # #1786: a shadow retired at its claim (off, moved head, closed PR) never
                # arrives; the merge check must stop waiting for it.
                self._refresh_check(None, row, 'cancelled')
                return None
            if read.read_error:
                self._claim_read_failed(con, row, current, read.read_error, now, read.since)
                return None
            if read.retry_read:
                con.execute('COMMIT')
                return None
            if read.unavailable:
                con.execute("UPDATE runs SET state='failed', attempts=attempts+1, "
                            "error=?,updated=? WHERE id=?", (read.unavailable, now, row['id']))
                con.execute("COMMIT")
                self._refresh_check(None, row, 'failed')        # #2326
                return None
            if merge_hold.spanned(con, row['repo'], row['pr'], now, read.fence):
                # #2468: a host merge push holds (or held) the PR: claimed now, the turn could
                # start at the head the push moves, so it waits for its next claim's reads.
                self.held_for_ci = True     # this worker lingers to schedule the retry (#1302)
                con.execute("UPDATE runs SET state='waiting', retry_at=?, error=?, updated=? "
                            "WHERE id=?", (now + merge_hold.WAIT_S, merge_hold.WAITING, now, row['id']))
                con.execute('COMMIT')
                self._refresh_check(None, row, 'waiting')       # its new reason shows (#2562)
                return None
            owner = uuid.uuid4().hex
            con.execute("UPDATE runs SET state='claimed', attempts=attempts+1, "
                        "owner=?, lease=?, updated=?, generation=? WHERE id=?",
                        (owner, now + self.lease_seconds, now, read.generation, row['id']))
            con.execute("COMMIT")
            return row['id'], owner

    def _run_production(self, run_id: str, owner: str) -> None:
        """Worker-only host control plane; never pass credentials to bwrap."""
        from .. import native_cleanup, native_lifecycle, pacing, trusted_turn
        turn = _Turn(run_id, owner, int(self.budget_of(run_id)))
        # An operator's stop (#1148: contained.Stopped, TurnCancelled) ends like any failed turn
        # here; complete_uncertain sees the request and makes it cancelled, never retried (or
        # uncertain after a write-ahead record).
        try:
            self._production_turn(turn)
        except trusted_turn.TurnBudgetExceeded as exc:
            turn.error = self._budget_kill_error(turn, exc)
            turn.retry = False
        except pacing.CapReached as exc:
            # Another turn took the last slot while this one staged (#1378): nothing launched.
            _cap_held(turn, exc.seat, exc.cap)
            turn.retry = False
        except native_cleanup.AdmissionRefused as exc:
            turn.error = f'native storage admission paused: {exc}'[:600]
            turn.paced_until = time.time() + run_supervisor.RETRY_BASE
            turn.retry = False
        except (run_supervisor.IssueIneligible, run_supervisor.ReviewHeadMoved) as exc:
            # Before launch, no model ran (#723): a cancellation, not a failure; never re-armed.
            # A review's moved head is delivered to the new head by the watchdog (#2468).
            turn.error, turn.cancel, turn.retry = str(exc)[:600], True, False
            from .. import fix_settle
            fix_settle.dropped_before_launch(turn.row, turn.error)     # #2248: logged only
        except native_lifecycle.CleanupIncomplete as exc:
            turn.error = f"native process completion unverified: {exc}"[:600]
            turn.stopped, turn.retry = False, False
        except Exception as exc:
            # The real reason, not just its type (#53). Messages here are host-generated
            # (no credential is ever formatted into one), and bounded.
            turn.error = (f"isolated turn failed: {type(exc).__name__}: {exc}"
                          f"{ledger.where(exc)}")[:600]
            turn.retry = run_supervisor.retryable(exc)
            if isinstance(exc, trusted_turn.TurnUnpublished):
                # The agent never called the broker (#144): nothing was refused or written, so
                # one retry, nudged, is safe. A second such exit is a person's to look at.
                turn.retry = not run_supervisor.unpublished_before(turn.row)
            if isinstance(exc, trusted_turn.TurnDenied) and 'did not shut down' in str(exc):
                turn.stopped = False  # a live broker thread may still write: quarantine
            if _native_unverified(exc):
                turn.stopped, turn.retry = False, False
        finally:
            self._production_finish(turn)

    def _production_turn(self, turn: _Turn) -> None:
        """The turn itself: take the row, gate the launch, build the turn, run it. A step that
        holds the turn sets ``turn.error`` and the turn ends there, nothing launched."""
        from .. import config, pacing, prompts, seat_model, trusted_turn
        assert self.production_config is not None
        try:
            settings = seat_model.load_runtime(self.production_config)
        except ValueError:
            raise ValueError("invalid production configuration") from None
        run_id, owner = turn.run_id, turn.owner
        with self._connect() as con:
            row = con.execute("SELECT * FROM runs WHERE id=? AND owner=?", (run_id, owner)).fetchone()
            if row is None:
                raise ValueError("run ownership lost")
            turn.row = row
            con.execute("UPDATE runs SET state='running', pid=?, lease=?, updated=? "
                        "WHERE id=? AND owner=? AND state='launching'",
                        (os.getpid(), time.time() + self.lease_seconds, time.time(), run_id, owner))
        loop = turn.loop = config.by_repo(row["repo"])
        if loop is None:
            raise ValueError("loop not configured")
        if row['seat'] == 'fixer' and (row['push_admitted'] != 1
                                       or not config.unattended_fixer_push_enabled(loop)):
            # The claim's policy read may be minutes old; a turn that cannot publish is
            # never started (the broker would refuse its push anyway).
            turn.error = run_supervisor.FIXER_NOT_ADMITTED if row['push_admitted'] != 1 else run_supervisor.FIXER_PUSH_REVOKED
            return
        # The seat claim and the head's in-flight mark, for as long as this run lives (#98):
        # the worker is the one party that knows a turn is running, so it writes them.
        # Seat claims and in-flight marks are PR-keyed (explain, the watchdog's clocks); a
        # triage run is issue-keyed and held by the ledger alone.
        # A shadow turn (#218) holds neither: it is not the PR's review, and a claim or an
        # in-flight mark would read as one (the gate's "a review is already out").
        turn.claim = (claims.claim_seat(loop, row, turn.budget)
                      if row['seat'] not in ('triage', 'issue_fixer', run_supervisor.SHADOW) else None)
        if row['seat'] == 'issue_fixer' and not config.issue_fixes_enabled(loop):
            # The policy may have changed since the gate: a turn that could not publish is
            # never started (the broker would refuse its write anyway). The cause names the
            # condition that failed, not the fixer-push one alone (#980).
            turn.error = run_supervisor.issue_fix_off_reason(loop) + run_supervisor.ISSUE_FIX_LAUNCH_SUFFIX
            return
        # The seat's own profile decides its model and account (#32). Resolved host-side,
        # before any GitHub read: an unresolvable seat is held here with the reason, and never
        # borrows another seat's model or key. The key lives only in this turn's proxy; an
        # OAuth seat's token is re-resolved there (host-side) when it nears expiry or is rejected.
        try:
            # An issue fix runs as the fixer seat (#214): its profile, its model, its account.
            model_seat = "fixer" if row["seat"] == "issue_fixer" else row["seat"]
            inference = seat_model.resolve_seat(loop, model_seat, settings)
        except seat_model.SeatModelError as exc:
            turn.error = f"seat model unresolved: {exc}"[:600]
            turn.retry = run_supervisor.retryable(exc)
            return
        provider = str(getattr(inference, "provider", "") or "the provider")
        account = pacing.account_key(provider, str(getattr(inference, "upstream", "") or ""),
                                     str(getattr(inference, "profile", "") or ""))
        ro_turn = self._pacing_hold(turn, provider, account)
        if turn.error:
            return
        if row['seat'] == 'reviewer':
            # Past run_supervisor.CI_WAIT_MAX_S only a red head with no fixer still holds (#1110).
            self._ci_hold(turn, expired=ci_wait_expired(row))
            if turn.error:
                return
        if row['seat'] == run_supervisor.SHADOW:
            self._shadow_ci_gate(turn)
            if turn.error:
                return
        launch = self._prepare_launch(turn)
        if launch is None:
            return
        self._launched(turn, prompts.revision(_turn_role(row)), inference.model)
        prompt = _nudged(row, launch.prompt)
        cancel = self._cancels.get(run_id)
        if cancel is not None and cancel.is_set():
            raise run_supervisor.TurnCancelled(run_supervisor.OPERATOR_STOPPED)  # before any launch
        # Each turn is counted once, against its own pool (#772), when its sandbox starts: a
        # turn that fails in staging or prefetch inside run_turn never spends one (#1378).
        counted_seat, cap = _turn_pool(loop, row, ro_turn)
        change, merged = launch.change, launch.merged
        turn.rc = trusted_turn.run_turn(loop, launch.scope, **runtime_detect.turn_paths(settings),
              upstream=inference.upstream, key=inference.key, model=inference.model,
              api_mode=inference.api_mode, credential=inference.credential_provider(),
              proxy_model=inference.proxy_model, client_identity=inference.client_identity,
              prompt=prompt, review_diff=change.diff if change else None,
              **({"merged": merged} if merged is not None else {}),
              timeout=turn.budget,
              # A shadow turn's broker authorizes with live reads and records; it never posts.
              no_write=row['seat'] == run_supervisor.SHADOW,
              # The seat profile's reasoning effort (#874), into the sandbox's Hermes config.
              reasoning_effort=getattr(inference, "reasoning_effort", "") or "",
              # Its own step budget, though its scope role is the reviewer's (#218).
              **({"steps": config.max_steps(loop, run_supervisor.SHADOW)} if row['seat'] == run_supervisor.SHADOW else {}),
              work_root=config.state_dir(loop) / "isolated-runs", observed=turn.observed,
              # The prefetch phase lands in the ledger as it happens (#51): a slow one shows
              # as "fetching", not as a silent turn, and its outcome outlives the run.
              progress=lambda text: self.record_dependencies(run_id, owner, text),
              # An operator's cancel (#1148) stops the sandbox the way the budget does.
              stop=cancel, on_launch=lambda: pacing.count_turn(loop['id'], counted_seat, cap=cap))
        self._account_after_turn(turn, row['seat'], provider, account)

    def _account_after_turn(self, turn: _Turn, seat: str, provider: str, account: str) -> None:
        """What a finished turn says about its account (#219, #1325). A 429 with a reset time, a
        provider-unavailable signature on a failed turn, or a run of provider errors (#1155)
        holds the account, and this run waits for it without spending a retry. A turn that
        succeeded clears an unavailable hold."""
        from .. import pacing
        # A non-zero sandbox exit with nothing on the write-ahead record is the model or
        # provider failing (429/5xx, OAuth refresh, crash): worth a backed-off retry.
        turn.retry = turn.rc != 0
        if turn.rc == 0:
            pacing.clear_unavailable(account)
            return
        done = seat == run_supervisor.SHADOW and self._shadow_verdict_recorded(turn)
        limited = turn.observed.get('rate_limited_until')
        if isinstance(limited, (int, float)) and limited > time.time():
            # The provider said its window is closed (#219): hold the account, and this run
            # waits for the reset instead of spending its retries against the 429.
            pacing.hold(account, limited, f"{provider} answered 429")
            until, reason = limited, ""
        else:
            # No 429 to say when (#1325): the DirectSDK path answers a closed window with empty
            # streams. Hold the account, for every seat on it, for a short growing window.
            # The same window runs.detail keeps: the capture holds DETAIL_MARGIN times more
            # (#2083) only so redaction sees a token at the cut whole.
            detail = pacing.unavailable_signature(
                (turn.observed.get('stdout') or '')[-run_supervisor.DETAIL_STDOUT_BYTES:],
                (turn.observed.get('stderr') or '')[-run_supervisor.DETAIL_STDERR_BYTES:])
            if detail is not None:
                until = pacing.hold_unavailable(account, detail)
            elif done or (until := _provider_strike(turn, account)) is None:
                return      # a shadow whose verdict is recorded is no provider failure (#1361)
            reason = (pacing.held(account) or (0, ""))[1]
        if done:
            return          # the account's hold stands for its next turn; this run is done (#1361)
        turn.paced_until = until
        turn.error = pacing.hold_line(seat, provider, until, reason)

    def _prepare_launch(self, turn: _Turn) -> _Launch | None:
        """What the turn is given: its prompt, its broker scope, and the change it sees.
        None when a step held the turn (``turn.error`` says why)."""
        from .. import config
        from ..broker import ipc as broker_ipc
        row, loop = turn.row, turn.loop
        if row['seat'] == 'triage':
            # An issue, not a PR (#213): no head, no checkout, no change record. The host
            # re-reads the issue right before launch and hands the model only its text.
            return _Launch(turn_prompts.triage_prompt(loop, row),
                           broker_ipc.RunScope(row["repo"], row["pr"], row["head"], "triage", "",
                                               row['id'], str(self.db)))
        if row['seat'] == 'issue_fixer':
            # An issue handed to the fixer (#214): the base commit it starts from is the
            # row's head; its one write creates a fresh branch from it.
            return _Launch(turn_prompts.issue_fix_prompt(loop, row),
                           broker_ipc.RunScope(row["repo"], row["pr"], row["head"], "issue_fixer",
                                               config.ISSUE_FIX_BRANCH.format(number=row["pr"]),
                                               row['id'], str(self.db)))
        return self._prepare_pr_launch(turn)

    def _prepare_pr_launch(self, turn: _Turn) -> _Launch | None:
        from .. import ci_fix, gh
        row, loop = turn.row, turn.loop
        pr = gh.api(loop, f'/repos/{row["repo"]}/pulls/{row["pr"]}', login=loop["read_token"])
        if not isinstance(pr, dict):
            raise run_supervisor.RetryableError("PR unreadable before launch (GitHub read failed)")
        head = pr.get("head")
        if not isinstance(head, dict):
            raise ValueError("PR head moved")
        if head.get("sha") != row["head"]:
            raise pr_facts.head_moved(row)
        if row['seat'] == 'reviewer' and not row['generation']:
            raise ValueError('review generation not durably pinned')
        if row['seat'] == 'fixer' and str(row['turn_key'] or '').startswith(run_supervisor.CONFLICT_KEY):
            return self._prepare_conflict_launch(turn, head)
        if row['seat'] == 'fixer' and ci_fix.is_ci_fix(row['turn_key']):
            return self._prepare_ci_fix_launch(turn, head)
        if row['seat'] == 'fixer' and str(row['turn_key'] or '').startswith(run_supervisor.SHADOW_FIX_KEY):
            return self._prepare_shadow_fix_launch(turn, head)
        return self._prepare_verdict_launch(turn, head)

    def _policy_hold(self, turn: _Turn) -> bool:
        """Every write this fixer turn would make is refused (#81): hold it instead of
        launching one. Named with the same reason as the broker's denial and the gate's queue
        hold; pre-write, so `retry` re-admits under the policy in force. Sets ``turn.error``."""
        from ..broker import ipc as broker_ipc
        row = turn.row
        turn.error = broker_ipc.policy_hold_reason(
            turn.loop, run_id=turn.run_id, repo=row['repo'], number=row['pr'], head=row['head'],
            ledger_db=str(self.db)) or None
        return turn.error is not None

    def _prepare_conflict_launch(self, turn: _Turn, head: dict) -> _Launch | None:
        # #303 stage 3: a conflict-resolution turn. No verdict is answered: the host
        # merges the pinned base into the head and the seat resolves the conflicts.
        from ..broker import ipc as broker_ipc
        row, loop = turn.row, turn.loop
        if self._policy_hold(turn):
            return None
        if owed := _conflict_waits(loop, row):            # #1907: a verdict since the claim
            raise run_supervisor.RetryableError(f"conflict turn waits: {owed}")
        merged = run_supervisor.conflict_merge(loop, row, head["ref"])
        if self._settle_generated(turn, head, merged):
            return None
        return _Launch(
            turn_prompts.conflict_prompt(loop, row, merged),
            broker_ipc.RunScope(
                row["repo"], row["pr"], row["head"], row["seat"], head["ref"], row['id'],
                str(self.db), row['generation'],
                merge={"base_ref": loop["base"],
                       "base_sha": row['turn_key'][len(run_supervisor.CONFLICT_KEY):],
                       "tree": merged["tree"]}),
            merged=merged)

    def _settle_generated(self, turn: _Turn, head: dict, merged: dict) -> bool:
        """#551: a conflict confined to registered generated files is settled here, with no
        fixer turn. The loop's generator runs in the sandbox on the merged tree (``generated``),
        and the result is pushed as the merge commit through the broker's own push path
        (``safe_push.push``: live authorization, exact-head lease, intent and confirmation in the
        ledger). True = the run is done (``turn.rc`` 0). False = the normal turn runs: the
        shortcut is off, a non-registered path conflicts, the generator failed, or the push was
        refused before it wrote anything (each logged). A push that may have written is
        quarantined, exactly as the broker's."""
        from .. import broker, config, generated, safe_push, seat_model
        row, loop = turn.row, turn.loop
        if generated.conflicted_entries(loop, merged['conflicted']) is None:
            return False
        try:
            settings = seat_model.load_runtime(self.production_config)
            files = generated.regenerate(loop, merged, settings,
                                         work_root=config.state_dir(loop) / 'isolated-runs')
        except Exception as exc:
            util.log(f'generated files: not run ({type(exc).__name__}: {exc}); normal conflict turn')
            return False
        if not files:
            return False
        scope = {'base_ref': loop['base'], 'base_sha': row['turn_key'][len(run_supervisor.CONFLICT_KEY):],
                 'tree': merged['tree']}
        manifest = generated.manifest(row['head'], loop['base'], files)
        args = (row['id'], row['repo'], row['pr'], row['head'])
        with config.push_policy_lock():
            current = config.by_repo(row['repo'])
            if (current is None or not config.unattended_fixer_push_enabled(current)
                    or not self.push_admitted(*args)):
                util.log('generated files: fixer push not authorized; normal conflict turn')
                return False
            self.begin_push(*args)
            try:
                safe_push.push(current, repo=row['repo'], number=row['pr'], head=row['head'],
                               role='fixer', branch=head['ref'], manifest=manifest, merge=scope)
                self.confirm_push(*args)
            except safe_push.scope_lock.ScopeHold:
                self.abort_push(*args)
                util.log('generated files: push held by the scope lock; normal conflict turn')
                return False
            except Exception as exc:
                if isinstance(exc, safe_push.PushFailure) or not isinstance(
                        exc, broker.BrokerDenied):
                    outcome = safe_push.held_outcome(exc)
                    self.quarantine_push(*args, outcome)
                    turn.stopped = True
                    turn.error = f'post-write push quarantine: {outcome}'
                    return True
                self.abort_push(*args)
                util.log(f'generated files: push refused ({exc}); normal conflict turn')
                return False
        turn.rc = 0
        util.log(f"generated files: settled {', '.join(sorted(files))} for PR #{row['pr']} "
                 'without a fixer turn')
        return True

    def _prepare_ci_fix_launch(self, turn: _Turn, head: dict) -> _Launch | None:
        # #306: a failed required check at this head, handed to the fixer. No verdict
        # is answered; CI is re-read now, and a green or moved head ends the run.
        from .. import ci, ci_fix, config, gh, prompts
        from ..broker import ipc as broker_ipc
        row, loop = turn.row, turn.loop
        if self._policy_hold(turn):
            return None
        if not config.fix_ci(loop):
            raise ValueError('CI fixes are off for this loop')
        state = ci.read(loop, row['head'])
        if state is None:
            raise run_supervisor.RetryableError('CI unreadable before launch (GitHub read failed)')
        names = ci_fix.failing(state, config.required_checks(loop))
        if not names:
            raise ValueError('CI no longer failing at this head')
        prompt = (prompts.render_isolated('ci_fix', repo=row['repo'], pr=row['pr'],
                                          url=gh.pr_url(loop, row['pr']),
                                          head=row['head'])
                  + prompts.fixer_check_section(loop, ci_fix=True)
                  + prompts.registry_section(None)
                  + ci_fix.section(loop, state, names))
        return _Launch(prompt, broker_ipc.RunScope(
            row["repo"], row["pr"], row["head"], row["seat"], head["ref"], row['id'],
            str(self.db), row['generation'], ci_fix=True))

    def _prepare_verdict_launch(self, turn: _Turn, head: dict) -> _Launch | None:
        """A review, a fix answering changes requested, or a ruling: the seat sees the PR's
        record (and, reviewer and fixer, the change itself)."""
        from .. import gh
        row, loop = turn.row, turn.loop
        reviews, marker = None, None
        if row['seat'] == 'fixer':
            from .. import gate
            reviews = pr_facts.effective_reviews(loop, row, gh.reviews(loop, row['pr']), self.db)
            if not isinstance(reviews, list):
                raise run_supervisor.RetryableError('reviews or receipts unreadable before launch')
            latest = gate.latest_effective_review_at_head(reviews, loop, row['head'])
            if latest is None or gh.review_state(latest) != 'CHANGES_REQUESTED':
                raise ValueError('fixer verdict no longer current')
            if self._policy_hold(turn):
                return None
        elif row['seat'] == 'adjudicator':
            # Again before launch: the claim's reads may be minutes old; a moved or approved head's ruling is noise.
            status, facts = pr_facts.adjudication_state(loop, row, self.db)
            if status in ('retry', 'wait'):
                raise run_supervisor.RetryableError('adjudication facts unreadable before launch')
            if status != 'ok':
                raise ValueError('adjudication no longer current')
            reviews, marker = facts['reviews'], facts['marker']
        else:
            # A fresh review after a retarget starts from nothing: old verdicts are
            # neither its round count nor its PR record.
            reviews = pr_facts.effective_reviews(loop, row, gh.reviews(loop, row['pr']), self.db)
            if row['seat'] == run_supervisor.SHADOW and isinstance(reviews, list):
                # The shadow sees what the real reviewer saw when both were queued (#218): a
                # review posted since, the real one's included, would tell it the answer.
                reviews = [r for r in reviews if _before_shadow_queued(r, row['created'])]
        seen: list = []
        prompt, prompt_row, change, shadow = self._verdict_prompt(loop, row, reviews, marker, seen)
        # Host-owned, before launch (#93, #110): whether this seat sees the whole change, in
        # the ledger (explain, the receipt claim) and in the scope the broker is built from.
        # Nothing inside the namespace can reach either.
        partial = change.partial if change else ''
        self.record_view(turn.run_id, turn.owner, partial)
        scope = self._verdict_scope(row, prompt_row, head, partial, shadow)
        if row['seat'] == 'adjudicator':
            from .. import state as state_mod
            self.record_saw(turn.run_id, turn.owner, seen)     # what its ACCEPT may dismiss
            # Last step before launch: mark the breach as being ruled on. Anyone else's
            # claim (a legacy gateway route included) refuses this one.
            # Stamped with this launch (#1767), so only this attempt's end hands it back.
            attempt = claims.attempt_of(row['id'], row['launch_intent'])
            if state_mod.state_for(loop).breach_start(row['pr'], row['head'], marker['rounds'],
                                                     attempt=attempt) is None:
                raise ValueError('breach marker already claimed or replaced')
            turn.breach = (state_mod.state_for(loop), marker['rounds'], attempt)
        return _Launch(prompt, scope, change=change)

    @staticmethod
    def _verdict_prompt(loop: dict, row, reviews, marker, seen: list | None = None) -> tuple:
        """The verdict seat's prompt, and what the launch needs from building it: the row it was
        prompted as, the change it saw and the shadow review a ruling's prompt showed. A ruling's
        prompt also fills ``seen`` with the reviewer reviews it showed (#2402)."""
        from .. import config
        # The reviewer and fixer see the change itself (#50); the adjudicator needs both sides' comments. A read
        # that failed is transient (retry); a moved head is not. The shadow is prompted as the reviewer it shadows.
        prompt_row = dict(row, seat='reviewer') if row['seat'] == run_supervisor.SHADOW else row
        try:
            # The last allowed attempt degrades an unreadable file list (#110): a stated partial view, not no turn.
            final = (row['retries'] or 0) + 1 >= run_supervisor.MAX_RETRIES
            change = (pr_facts.pr_change(loop, prompt_row, final=final)
                      if prompt_row['seat'] in ('reviewer', 'fixer') else None)
            # A shadow escalation's ruling answers the shadow review read HERE (#1112): the prompt
            # shows it and the scope records its run id, from one read, so a later one is never shown.
            shadow, extra = None, {}
            if (row['seat'] == 'adjudicator'
                    and ((marker or {}).get('escalated') or {}).get('by') == 'shadow'):
                shadow = turn_prompts.newest_shadow_review(row, missing=False)
                if shadow is False:     # superseded (#2601): wait, bounded, for its own (#2688)
                    raise run_supervisor.RetryableError("waiting on the active reviewer turn's shadow review")
                if shadow is None:      # #1145: a ruling on none records '': wait for a read
                    raise run_supervisor.RetryableError('shadow review unreadable before launch')
                if str(shadow[2]) != 'REQUEST_CHANGES':  # agrees now: no ruling (shadow_retired drops its hold)
                    raise ValueError(SHADOW_AGREES)
                extra = {'shadow': shadow}
            if row['seat'] == 'adjudicator':
                extra['seen'] = seen
            elif row['seat'] == run_supervisor.SHADOW:
                # Prompted as the reviewer, but told its own step budget (#2199).
                extra = {'steps': config.max_steps(loop, run_supervisor.SHADOW)}
            prompt = turn_prompts.isolated_prompt(loop, prompt_row, reviews, marker, change, **extra)
        except ValueError as exc:
            if str(exc) in ('fixer answers unreadable', 'PR unreadable') \
                    or str(exc).startswith('PR files unreadable'):
                raise run_supervisor.RetryableError(str(exc)) from None
            raise
        return prompt, prompt_row, change, shadow

    def _verdict_scope(self, row, prompt_row, head: dict, partial: str, shadow):
        """The broker scope of a review, fix or ruling turn, built from the ledger row. It names
        the run's own turn key, so a shadow review is judged on the findings snapshot kept for
        its reviewer turn and never another's at the same PR and head, and refused with none
        (``shadow``, #1139). An adjudicator's
        also records the id of the shadow review its prompt showed (``shadow``, #1112)."""
        from ..broker import ipc as broker_ipc
        return broker_ipc.RunScope(row["repo"], row["pr"], row["head"],
                                   prompt_row["seat"], head["ref"], row['id'],
                                   str(self.db), row['generation'], partial_view=partial,
                                   shadow_run=((shadow[0] if shadow else '')
                                               if row['seat'] == 'adjudicator' else None),
                                   turn_key=row['turn_key'] or '',
                                   shadow=row['seat'] == run_supervisor.SHADOW)

    def _launched(self, turn: _Turn, prompt_rev: str, model: str) -> None:
        """Record what the turn launches with (#472), then show it running on its PR's merge
        check (#753) before the seat starts."""
        self.record_launch(turn.run_id, turn.owner, prompt_rev, model)
        turn.launched = True
        self._refresh_check(turn.loop, turn.row, 'running', launched=True)

    def _refresh_check(self, loop: dict | None, row, state: str | None,
                       launched: bool = False) -> None:
        """Refresh the merge check of a run's own PR once the ledger says something new about it.

        A shadow run (#1786): the check says "waiting on the shadow review" for as long as the
        ledger shows the run open, so it is refreshed once the run has ended; one still open
        (``gate.shadows.SHADOW_OPEN``: held for CI, waiting to retry) changes nothing. Any other PR
        turn (#753): the check names the turn (``views.turn_note``), so it is refreshed when the
        turn launches and when it ends, except a turn that never launched and waits again with the
        reason it was claimed with: a CI hold re-polled unchanged costs no refresh. ``state``
        None means this owner no longer holds the run. A ``loop`` of None is looked up from the
        row's repo. Best effort, like every refresh after an event."""
        try:
            from .. import config, gate, merge_check
            from . import claims
            if (row is None or state is None
                    or row['seat'] not in (*claims.PR_HOLD_SEATS, run_supervisor.SHADOW)):
                return                  # an issue fix's or a triage's number is an issue's
            if row['seat'] == run_supervisor.SHADOW and state in gate.shadows.SHADOW_OPEN:
                return
            if row['seat'] != run_supervisor.SHADOW and state == 'waiting' and not launched:
                with self._connect() as con:
                    now = con.execute("SELECT error FROM runs WHERE id=?", (row['id'],)).fetchone()
                if now is not None and now[0] == row['error']:
                    return
            loop = loop or config.by_repo(row['repo'])
            if loop is not None:
                merge_check.refresh_quietly(loop, row['pr'])
        except Exception:
            pass    # a lost refresh must not change how the turn ends

    def _budget_kill_error(self, turn: _Turn, exc) -> str:
        # Name the clock (#49): an opaque "TimeoutExpired" hides that a setting killed the
        # turn. Never retried automatically: the same budget would most likely run out
        # again, each attempt burning a whole budget.
        killed = (f"isolated turn failed: TimeoutExpired — killed at the {exc.budget}s turn "
                  f"budget (sandbox stopped {exc.grace}s past it)")
        # The broker drain lets a write already in flight finish after the kill (#98): a
        # push can complete, a receipt be recorded. Then the run wrote — final, and the
        # "raise the budget, then retry" advice would be wrong (`retry` refuses it).
        with self._connect() as con:
            wrote = run_supervisor.write_evidence(con, turn.run_id)
        if wrote == 'review receipt claimed':
            # Sent but never read back: the run goes uncertain (reconcile), not final.
            return (f"{killed} after it may have written ({wrote}) — never replayed; "
                    "inspect the PR and reconcile")
        if wrote is not None:
            return (f"{killed} after it wrote ({wrote}) — final: never replayed; a new head "
                    "gets a fresh turn")
        flag = {'reviewer': '--reviewer-turn-budget',
                'fixer': '--fixer-turn-budget'}.get(turn.row['seat'], '--turn-budget')
        return (f"{killed} — raise turn_budget_s (hermes dk set --loop "
                f"{turn.loop['id']} {flag} N), then `retry`")

    def _production_finish(self, turn: _Turn) -> None:
        """However the turn ended: its notes and usage, its final state, the breach marker
        handed back if it made no ruling, the seat released, a failure noticed."""
        run_id, owner, observed = turn.run_id, turn.owner, turn.observed
        try:
            self.record_thinking(run_id, owner, observed.get('thinking'))
        except Exception:
            pass    # a lost note must not change how the turn ends
        try:
            self.record_usage(run_id, owner, observed, turn.loop['id'],
                              'fixer' if turn.row['seat'] == 'issue_fixer' else turn.row['seat'])
        except Exception:
            pass    # likewise: a lost count must not change how the turn ends
        shadow = turn.row is not None and turn.row['seat'] == run_supervisor.SHADOW
        recorded = False
        if shadow:
            # A shadow is a sample, never stuck work: a failed one is not retried (#218: the
            # first live one spent four whole turns' tokens to record nothing). A closed usage
            # window still waits for its reset, which costs nothing.
            turn.retry = False
            # #1361: a shadow whose verdict is recorded did its job, so it succeeds; the exit
            # is kept.
            recorded = (self._shadow_verdict_recorded(turn) and turn.rc not in (None, 0)
                        and turn.error is None)
            if recorded:
                turn.error = (f"verdict recorded, then the turn exited with status {turn.rc} "
                              "(its output is in the run's detail)")
        state = self.complete_uncertain(
            run_id, owner, turn.rc, turn.error, stopped=turn.stopped, retry=turn.retry,
            detail=run_supervisor.output_detail(observed.get('stdout'), observed.get('stderr')),
            paced_until=turn.paced_until, cancel=turn.cancel, recorded=recorded)
        waits.note(self, run_id)               # a paced, held or backed-off turn (#2080)
        if turn.breach is not None and state in ('waiting', 'failed', 'cancelled'):
            # This run marked the breach 'adjudicating' and made no ruling: hand the
            # marker back so its retry (or a re-arm) can claim it again.
            try:
                with self._connect() as con:
                    unwritten = run_supervisor.write_records(con, run_id) is None
                if unwritten:
                    turn.breach[0].breach_resume(turn.row['pr'], turn.row['head'], turn.breach[1],
                                                 attempt=turn.breach[2])
            except Exception:
                pass
        claims.release_seat(turn.claim, state)
        # The merge check reads the run's state from the ledger, so it is refreshed once that
        # state is written (#1786 for a shadow, #1163 for its recorded verdict, #753 for a turn
        # the check names), whatever the outcome.
        self._refresh_check(turn.loop, turn.row, state, launched=turn.launched)
        if not shadow:
            # A shadow turn is no part of the loop (#218): its failure pages nobody.
            self.failure_notice(run_id, state)
        self.recover()
