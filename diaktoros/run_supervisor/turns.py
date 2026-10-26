"""A pending run's claim and a claimed run's production turn (#758), moved out of the core.

``ClaimAndLaunch`` is a mixin: ``Supervisor`` inherits it, so ``Supervisor(db)._claim()`` and a
test's ``patch.object(Supervisor, ...)`` work as before, with one binding. The steps are the
claim reads (``_claim_read_*``), ``_claim_commit``, ``_production_turn`` with its holds, the
``_prepare_*_launch`` steps, ``_budget_kill_error`` and ``_production_finish``; their state is
``_ClaimRead``, ``_Turn`` and ``_Launch``, defined here. The mixin uses ``_connect()`` and the
rest of ``Supervisor``, and the package's names through the package (``run_supervisor.X``), so a
test patching ``run_supervisor.X`` still reaches this code.
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .. import ledger, run_supervisor, util
from . import pr_facts, turn_prompts


@dataclass
class _ClaimRead:
    """What a pending row's claim-time reads found; ``_claim_commit`` settles the row from it.
    ``retry_read`` is a legitimate wait (a draft), left pending. ``read_error`` is a read that
    failed: counted and backed off like a failed turn, then failed with its reason, so it can
    never sit pending and invisible (#53, review on #97). ``refused`` is a row that can never
    publish; ``unavailable``, one whose review generation cannot be pinned."""
    generation: str | None = None
    unavailable: str | None = None
    retry_read: bool = False
    read_error: str = ""
    superseded: str | None = None
    refused: str = ""


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


class _Launch(NamedTuple):
    """What a turn is launched with: its prompt, its broker scope, the change it reviews or
    fixes, and a conflict turn's merge."""
    prompt: str
    scope: object
    change: object = None
    merged: dict | None = None


class ClaimAndLaunch:
    """The claim and turn methods of :class:`~diaktoros.run_supervisor.Supervisor`."""

    def _claim(self):
        # A migration holds the install (#431): no run starts under names being moved. The
        # pending rows wait where they are; the worker that finds nothing here exits as usual.
        from .. import migrate
        if migrate.migrating() is not None:
            return None
        # Snapshot candidates without taking a writer lock. A slow GitHub read
        # must not block unrelated enqueues, heartbeats, or receipt commits.
        with self._connect() as con:
            candidates = con.execute("SELECT * FROM runs WHERE state='pending' "
                                     "ORDER BY created,id").fetchall()
        for row in run_supervisor.claim_order(candidates):
            read = _ClaimRead()
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
        return None

    def _claim_read_shadow(self, row, read: _ClaimRead) -> None:
        """A shadow row's claim-time read (#218): the PR still open, out of draft, at this head.
        Unlike the reviewer's, a review already at the head never supersedes it: the shadow
        exists to review the head the real reviewer reviewed. A moved or closed PR retires it."""
        from .. import config, gh
        try:
            loop = config.by_repo(row['repo'])
            if loop is None or not config.shadow_profile(loop):
                read.superseded = 'shadow reviews are off for this loop'
                return
            pr = gh.api(loop, f"/repos/{row['repo']}/pulls/{row['pr']}",
                        login=loop['read_token'])
            if not isinstance(pr, dict) or pr.get('number') != row['pr']:
                read.read_error = 'PR unreadable (GitHub read failed)'
            elif pr.get('state') != 'open' or (pr.get('head') or {}).get('sha') != row['head']:
                read.superseded = 'shadow review superseded: PR closed or head moved'
            elif pr.get('draft') is not False:
                read.retry_read = True
        except Exception as exc:
            read.read_error = f'{type(exc).__name__}: {exc}'[:200]

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
                read.superseded = 'PR head moved before the review started'
            elif pr.get('state') != 'open' or pr.get('draft') is not False:
                read.retry_read = True
            else:
                read.generation = generation_for(pr, loop, row['pr'], row['head'])
                # A review at this exact head already answers the turn (e.g. one
                # posted in the GitHub UI): a second would spend another round.
                # The same-head retarget's fresh-review turn exists to review
                # again at this head, so it is exempt.
                # A dispute re-review (#894) exists to review again at this head, too.
                if not str(row['turn_key'] or '').startswith(('retarget:', run_supervisor.DISPUTE_KEY)):
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
            # The operator escalated this head (#459): it waits for a ruling, so a review or
            # fix queued for it is retired, as a spent cap would have stopped it.
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
        if row['seat'] in run_supervisor.PR_HOLD_SEATS and not read.superseded:
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
                ruled = gate.upheld_shadow_ruling(loop, row['pr'], row['head'], db=self.db)
                if ruled is None or row['turn_key'] != f"{run_supervisor.SHADOW_FIX_KEY}{int(ruled)}":
                    read.superseded = 'upheld shadow ruling no longer current'
            elif (str(row['turn_key'] or '').startswith(run_supervisor.CONFLICT_KEY)
                  or ci_fix.is_ci_fix(row['turn_key'])):
                # #303: a conflict turn answers no verdict (the PR may be approved, or
                # unreviewed); the worker checks the merge itself before launch.
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
                return None
            # Recheck both capacity and PR occupancy under the writer lock. A shadow turn
            # (#218) neither occupies the PR nor waits for it: it writes nothing, so the real
            # reviewer and fixer never queue behind it, and it never queues behind them.
            occupied = row['seat'] != run_supervisor.SHADOW and con.execute(
                "SELECT 1 FROM runs WHERE repo=? AND pr=? AND " + run_supervisor.NOT_SHADOW + " "
                "AND (state IN ('claimed','launching','running','uncertain') "
                "OR push_intent IS NOT NULL)", (row['repo'], row['pr'])).fetchone()
            # Capacity is per repo and seat: another repo's rows never consume it.
            used = con.execute("SELECT COUNT(*) FROM runs WHERE repo=? AND seat=? AND "
                               "state IN ('claimed','launching','running','uncertain')",
                               (row['repo'], row['seat'])).fetchone()[0]
            if occupied or used >= self._capacity_for(row['repo'], row['seat']):
                con.execute("COMMIT")
                return None
            now = time.time()
            if read.superseded:
                con.execute("UPDATE runs SET state='cancelled', error=?,updated=? WHERE id=?",
                            (read.superseded, now, row['id']))
                con.execute('COMMIT')
                return None
            if read.read_error:
                # Bounded like a failed turn (#53): back off, then fail with the reason and
                # a notice. Pre-write, so `retry` (or a new event) re-arms it.
                retries = (current['retries'] or 0) + 1
                reason = f'claim-time read failed: {read.read_error}'
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
                return None
            if read.retry_read:
                con.execute('COMMIT')
                return None
            if read.unavailable:
                con.execute("UPDATE runs SET state='failed', attempts=attempts+1, "
                            "error=?,updated=? WHERE id=?", (read.unavailable, now, row['id']))
                con.execute("COMMIT")
                return None
            owner = uuid.uuid4().hex
            con.execute("UPDATE runs SET state='claimed', attempts=attempts+1, "
                        "owner=?, lease=?, updated=?, generation=? WHERE id=?",
                        (owner, now + self.lease_seconds, now, read.generation, row['id']))
            con.execute("COMMIT")
            return row['id'], owner

    def _run_production(self, run_id: str, owner: str) -> None:
        """Worker-only host control plane; never pass credentials to bwrap."""
        from .. import trusted_turn
        turn = _Turn(run_id, owner, int(self.budget_of(run_id)))
        try:
            self._production_turn(turn)
        except trusted_turn.TurnBudgetExceeded as exc:
            turn.error = self._budget_kill_error(turn, exc)
            turn.retry = False
        except run_supervisor.IssueIneligible as exc:
            # Before launch, no model ran (#723): a cancellation, not a failure; never re-armed.
            turn.error, turn.cancel, turn.retry = str(exc)[:600], True, False
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
        finally:
            self._production_finish(turn)

    def _production_turn(self, turn: _Turn) -> None:
        """The turn itself: take the row, gate the launch, build the turn, run it. A step that
        holds the turn sets ``turn.error`` and the turn ends there, nothing launched."""
        from .. import ci_fix, config, pacing, prompts, seat_model, trusted_turn
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
        turn.claim = (run_supervisor.claim_seat(loop, row, turn.budget)
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
            self._ci_hold(turn, expired=time.time() - row['created'] >= run_supervisor.CI_WAIT_MAX_S)
            if turn.error:
                return
        if row['seat'] == run_supervisor.SHADOW:
            self._shadow_ci_gate(turn)
            if turn.error:
                return
        launch = self._prepare_launch(turn)
        if launch is None:
            return
        if row['seat'] == 'fixer' and str(row['turn_key'] or '').startswith(run_supervisor.CONFLICT_KEY):
            role = 'conflict'
        elif row['seat'] == 'fixer' and ci_fix.is_ci_fix(row['turn_key']):
            role = 'ci_fix'
        else:
            # A shadow turn is told exactly what the reviewer is told (#218).
            role = 'reviewer' if row['seat'] == run_supervisor.SHADOW else row['seat']
        self.record_launch(run_id, owner, prompts.revision(role), inference.model)
        prompt = launch.prompt
        if run_supervisor.unpublished_before(row):
            prompt = run_supervisor.PUBLISH_NUDGE + prompt
        # Each turn is counted once, against its own pool (#772).
        pacing.count_turn(loop['id'], pacing.REVIEW_ONLY_SEAT if ro_turn else row['seat'])
        change, merged = launch.change, launch.merged
        turn.rc = trusted_turn.run_turn(loop, launch.scope, source=Path(settings["source"]),
              venv=Path(settings["venv"]), runtime=Path(settings["runtime"]),
              rust=Path(settings["rust"]), upstream=inference.upstream,
              key=inference.key, model=inference.model,
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
              progress=lambda text: self.record_dependencies(run_id, owner, text))
        # A non-zero sandbox exit with nothing on the write-ahead record is the model or
        # provider failing (429/5xx, OAuth refresh, crash): worth a backed-off retry.
        turn.retry = turn.rc != 0
        limited = turn.observed.get('rate_limited_until')
        if turn.rc != 0 and isinstance(limited, (int, float)) and limited > time.time():
            # The provider said its window is closed (#219): hold the account, and this run
            # waits for the reset instead of spending its retries against the 429.
            pacing.hold(account, limited, f"{provider} answered 429")
            turn.paced_until = limited
            turn.error = (f"held: {row['seat']} usage window ({provider}) — resumes "
                          f"{pacing.when(limited)}")

    def _pacing_hold(self, turn: _Turn, provider: str, account: str) -> bool:
        """Pacing (#219): an account whose usage window is closed, or a seat past its daily
        cap, waits for the reset instead of launching a turn that would only 429. Sets
        ``turn.error`` when held; answers whether this is a review-only turn."""
        from .. import config, gh, pacing
        row, loop = turn.row, turn.loop
        closed = pacing.held(account)
        if closed is not None:
            turn.paced_until = closed[0]
            turn.error = (f"held: {row['seat']} usage window ({provider}) — resumes "
                          f"{pacing.when(closed[0])}")
            return False
        # Two pools (#772, #457): with review_only_daily set, a review-only PR's reviewer turn
        # counts against that cap only, every other turn against daily_turns only. Unset,
        # review-only turns share daily_turns. So the author is classified before any turn cap.
        ro_cap = config.review_only_daily(loop) if row['seat'] == 'reviewer' else None
        ro_turn = False
        if ro_cap is not None and config.review_only(loop):
            live_pr = gh.api(loop, f'/repos/{row["repo"]}/pulls/{row["pr"]}',
                             login=loop["read_token"])
            if not isinstance(live_pr, dict):
                raise run_supervisor.RetryableError("PR unreadable before launch (GitHub read failed)")
            ro_author = str(((live_pr.get("user") or {}).get("login")) or "").lower()
            ro_turn = ro_author in config.review_only(loop)
        if ro_turn:
            if pacing.turns_today(loop['id'], pacing.REVIEW_ONLY_SEAT) >= ro_cap:
                turn.paced_until = pacing.next_midnight()
                turn.error = (f"held: review-only daily turn cap ({ro_cap}) reached — resumes "
                              f"{pacing.when(turn.paced_until)}")
                return ro_turn
        else:
            cap = config.seat_daily_turns(loop, row['seat'])
            if cap is not None and pacing.turns_today(loop['id'], row['seat']) >= cap:
                turn.paced_until = pacing.next_midnight()
                turn.error = (f"held: {row['seat']} daily turn cap ({cap}) reached — resumes "
                              f"{pacing.when(turn.paced_until)}")
                return False
        # A seat's daily token budget (#480): once spent, the next turn waits for midnight.
        # A turn in flight is never killed; this only gates a launch. The fixer's budget
        # covers its issue fixes too (seat_daily_tokens, and record_usage's counter key).
        budget_seat = 'fixer' if row['seat'] == 'issue_fixer' else row['seat']
        token_cap = config.seat_daily_tokens(loop, row['seat'])
        if token_cap is not None and pacing.tokens_today(loop['id'], budget_seat) >= token_cap:
            turn.paced_until = pacing.next_midnight()
            turn.error = (f"held: {row['seat']} daily token budget ({token_cap}) reached — "
                          f"resumes {pacing.when(turn.paced_until)}")
            return False
        return ro_turn

    def _ci_hold(self, turn: _Turn, expired: bool = False) -> None:
        """A reviewer turn held for CI, nothing spent (sets ``turn.error``). A cancelled check
        (#363) holds every review: the broker would refuse its APPROVE, so the turn could only
        spend tokens. Running checks hold it only with review_after_ci (#241), even beside a
        failed one. A failed check holds while the fixer takes it first (#539); then, and on a
        review-only PR, which no fixer pushes to, it holds until the checks pass (a rerun) or a
        new head supersedes it (#1110): a verdict that only says "CI failed" would spend a round
        and close the head to re-review. Only where CI fixes are off for a fixer's PR does the
        review run, since it is how the fixer hears of the failure. ``expired``: past
        ``run_supervisor.CI_WAIT_MAX_S`` only that red hold remains."""
        from .. import ci, ci_fix, config, gh
        row, loop = turn.row, turn.loop
        # A PR that conflicts with its base gets no pull_request CI from GitHub, so the
        # required checks could never report: hold, nothing spent. The merge push makes
        # a new head that supersedes this row. unknown/null mergeability is no conflict.
        queued = gh.api(loop, f'/repos/{row["repo"]}/pulls/{row["pr"]}',
                        login=loop["read_token"])
        # A closed or draft PR is left to the eligibility checks below, which supersede it.
        if not expired and run_supervisor._conflicts_at_head(queued, row["head"]):
            base = queued.get("base")
            base = str(base.get("ref") or "") if isinstance(base, dict) else ""
            turn.paced_until = time.time() + run_supervisor.CI_POLL_S
            turn.error = (f"{run_supervisor.CI_HOLD} on {row['head'][:7]} — conflicts with "
                          f"{base or 'its base'} — GitHub runs no CI on it")[:600]
            self.held_for_ci = True
            return
        author = (str(((queued.get("user") or {}).get("login")) or "")
                  if isinstance(queued, dict) else "")
        review_only = author.lower() in config.review_only(loop)
        # Red CI no review answers: no fixer pushes here, or the fixer's CI fixes are done.
        holdable = review_only or ci_fix.fixable(loop, author)
        if expired and not holdable:
            return                      # past the wait, only that red hold remains
        after_ci = config.review_after_ci(loop)
        # Only the required checks hold it, when the loop names them (#368).
        checks = ci.gating(ci.read(loop, row['head']), config.required_checks(loop))
        # #306/#539/#579: a red head goes to the fixer first while its CI-fix budget
        # lasts, whatever review_after_ci says (queued right here, not left to the next
        # sweep): no review verdict is spent on code that does not build. Budget spent, or this head's turn
        # done without a new head: it waits for green or a new head, like a review-only PR's (#1110).
        fixing = bool(checks is not None and checks.failed and not expired
                      and ci_fix.fixer_first(loop, number=row['pr'], head=row['head'],
                                             author=author, failed=checks.failed,
                                             db=self.db, log=util.log))
        red = bool(checks is not None and checks.failed and holdable and not fixing)
        if expired and not red:
            return
        # #374: a failure releases the cancelled/missing holds only (the review would
        # otherwise go unreported); the pending wait survives it, so one review names
        # every failure once CI finishes.
        if checks is not None and (
                fixing or red
                or (not checks.failed and (checks.cancelled or checks.missing))
                or (after_ci and checks.pending)):
            # Nothing spent: no model call, no daily turn, no retry. This worker stays
            # to re-queue it (linger_for_ci); the watchdog sweep is the backstop.
            turn.paced_until = time.time() + run_supervisor.CI_POLL_S
            turn.error = (f"{run_supervisor.CI_HOLD} on {row['head'][:7]} — "
                          + (f"{len(checks.cancelled)} check(s) cancelled, re-run them on "
                             f"GitHub ({ci._names(checks.cancelled[:5])})"
                             if checks.cancelled and not red else
                             f"{len(checks.failed)} check(s) failed, the fixer is taking them first"
                             if fixing else
                             f"{len(checks.failed)} required check(s) failed "
                             f"({ci._names(checks.failed[:5])}) — "
                             + ("no fixer takes a review-only PR" if review_only else
                                "the fixer's CI fixes are done")
                             + ": the review starts when they pass (re-run them on GitHub) or a "
                             "new head is pushed"
                             if red else
                             f"{len(checks.missing)} required check(s) never reported "
                             f"({ci._names(checks.missing[:5])}); check the name"
                             if checks.missing else
                             f"{len(checks.pending)} check(s) still running or not reported"))[:600]
            self.held_for_ci = True

    def _shadow_escalation(self, turn: _Turn) -> None:
        """Right after a shadow review is recorded (#1004 part 2): if it disagrees with the
        reviewer's approval at the head, send the head for a ruling now. Best effort: the
        watchdog sweep makes the same check, so a failure here only delays it."""
        from .. import gate, gh, state as state_mod, transition
        row, loop = turn.row, turn.loop
        if row is None or loop is None:
            return
        st = state_mod.state_for(loop)
        reviews = transition.effective_reviews(loop, st, row['pr'], row['head'],
                                               gh.reviews(loop, row['pr']))
        outcome = gate.shadow_escalation(loop, st, row['pr'], row['head'], reviews, db=self.db)
        if outcome == "new":
            util.log(f"#{row['pr']} @ {row['head'][:7]} shadow review disagrees with the "
                     "approval: sent for a ruling")

    def _shadow_ci_gate(self, turn: _Turn) -> None:
        """A shadow review follows the reviewer's CI rules, strictly (#218): it waits, nothing
        spent, while the PR conflicts or a required check is running, cancelled, missing or
        unreadable; and it never reviews a red head, which it gives up (cancelled). Stricter
        than the reviewer on purpose: running checks hold it whatever ``review_after_ci`` says,
        a red head is never reviewed (the reviewer's is, once the fixer's CI-fix budget is
        spent), and past ``run_supervisor.CI_WAIT_MAX_S`` with CI unfinished it gives the head up rather than
        review it. A shadow verdict is a sample of a model, so it is taken only on code CI
        passed. Sets ``turn.error``."""
        from .. import ci, config, gh
        row, loop = turn.row, turn.loop
        head = row['head']

        def hold(why: str) -> None:
            if time.time() - row['created'] >= run_supervisor.CI_WAIT_MAX_S:
                turn.cancel = True
                turn.error = (f"shadow review skipped: CI did not finish at {head[:7]} within "
                              f"{run_supervisor.CI_WAIT_MAX_S // 60} min ({why})")[:600]
                return
            turn.paced_until = time.time() + run_supervisor.CI_POLL_S
            turn.error = f"{run_supervisor.CI_HOLD} on {head[:7]} — {why}"[:600]
            self.held_for_ci = True

        queued = gh.api(loop, f'/repos/{row["repo"]}/pulls/{row["pr"]}', login=loop["read_token"])
        # The claim's eligibility, re-read here because the PR may have changed since the
        # claim (#1003): a closed PR retires the row, a draft waits for ready (the claim's
        # wait) however old the row is, and neither is ever sampled or held as a conflict.
        if isinstance(queued, dict) and (queued.get("head") or {}).get("sha") == head:
            if queued.get("state") != "open":
                turn.cancel = True
                turn.error = "shadow review superseded: PR closed"
                return
            if queued.get("draft") is not False:
                turn.paced_until = time.time() + run_supervisor.CI_POLL_S
                turn.error = f"{run_supervisor.CI_HOLD} on {head[:7]} — the PR is a draft, waiting for ready"
                self.held_for_ci = True
                return
        if run_supervisor._conflicts_at_head(queued, head):
            return hold("conflicts with its base, so GitHub runs no CI on it")
        checks = ci.gating(ci.read(loop, head), config.required_checks(loop))
        if checks is None:
            return hold("its checks could not be read")
        if checks.failed:
            turn.cancel = True
            turn.error = (f"shadow review skipped: CI failed at {head[:7]} "
                          f"({ci._names(checks.failed[:5])}); a shadow never reviews a red "
                          "head — the fixed head gets its own")[:600]
            return
        if checks.cancelled:
            return hold(f"{len(checks.cancelled)} check(s) cancelled")
        if checks.missing:
            return hold(f"{len(checks.missing)} required check(s) never reported")
        if checks.pending:
            return hold(f"{len(checks.pending)} check(s) still running")

    def _prepare_launch(self, turn: _Turn) -> _Launch | None:
        """What the turn is given: its prompt, its broker scope, and the change it sees.
        None when a step held the turn (``turn.error`` says why)."""
        from .. import broker_ipc, config
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
        if not isinstance(head, dict) or head.get("sha") != row["head"]:
            raise ValueError("PR head moved")
        if row['seat'] == 'reviewer' and not row['generation']:
            raise ValueError('review generation not durably pinned')
        if row['seat'] == 'fixer' and str(row['turn_key'] or '').startswith(run_supervisor.CONFLICT_KEY):
            return self._prepare_conflict_launch(turn, head)
        if row['seat'] == 'fixer' and ci_fix.is_ci_fix(row['turn_key']):
            return self._prepare_ci_fix_launch(turn, head)
        if row['seat'] == 'fixer' and str(row['turn_key'] or '').startswith(run_supervisor.SHADOW_FIX_KEY):
            return self._prepare_shadow_fix_launch(turn, head)
        return self._prepare_verdict_launch(turn, head)

    def _prepare_shadow_fix_launch(self, turn: _Turn, head: dict) -> _Launch | None:
        """#1004 part 3: the fixer answers a shadow review the adjudicator upheld. The ruling is
        re-read now (a moved head or a later ruling ends the run), and the push is allowed only
        while it still stands (``RunScope.shadow_fix``, checked by the broker)."""
        from .. import broker_ipc, gate, gh
        row, loop = turn.row, turn.loop
        ruled = gate.upheld_shadow_ruling(loop, row['pr'], row['head'], db=self.db)
        if ruled is None or row['turn_key'] != f"{run_supervisor.SHADOW_FIX_KEY}{int(ruled)}":
            raise ValueError('upheld shadow ruling no longer current')
        if self._policy_hold(turn):
            return None
        reviews = pr_facts.effective_reviews(loop, row, gh.reviews(loop, row['pr']), self.db)
        if not isinstance(reviews, list):
            raise run_supervisor.RetryableError('reviews or receipts unreadable before launch')
        try:
            final = (row['retries'] or 0) + 1 >= run_supervisor.MAX_RETRIES
            change = pr_facts.pr_change(loop, row, final=final)
            prompt = (turn_prompts.isolated_prompt(loop, row, reviews, None, change)
                      + turn_prompts.upheld_shadow_section(row, ruled))
        except ValueError as exc:
            if str(exc) == 'PR unreadable' or str(exc).startswith('PR files unreadable'):
                raise run_supervisor.RetryableError(str(exc)) from None
            raise
        partial = change.partial if change else ''
        self.record_view(turn.run_id, turn.owner, partial)
        return _Launch(prompt, broker_ipc.RunScope(
            row["repo"], row["pr"], row["head"], row["seat"], head["ref"], row['id'],
            str(self.db), row['generation'], partial_view=partial, shadow_fix=ruled),
            change=change)

    def _policy_hold(self, turn: _Turn) -> bool:
        """Every write this fixer turn would make is refused (#81): hold it instead of
        launching one. Named with the same reason as the broker's denial and the gate's queue
        hold; pre-write, so `retry` re-admits under the policy in force. Sets ``turn.error``."""
        from .. import broker_ipc
        row = turn.row
        turn.error = broker_ipc.policy_hold_reason(
            turn.loop, run_id=turn.run_id, repo=row['repo'], number=row['pr'], head=row['head'],
            ledger_db=str(self.db)) or None
        return turn.error is not None

    def _prepare_conflict_launch(self, turn: _Turn, head: dict) -> _Launch | None:
        # #303 stage 3: a conflict-resolution turn. No verdict is answered: the host
        # merges the pinned base into the head and the seat resolves the conflicts.
        from .. import broker_ipc
        row, loop = turn.row, turn.loop
        if self._policy_hold(turn):
            return None
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
        from .. import broker_ipc, ci, ci_fix, config, gh, prompts
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
                  + prompts.fixer_check_section(loop)
                  + prompts.registry_section(None)
                  + ci_fix.section(loop, state, names))
        return _Launch(prompt, broker_ipc.RunScope(
            row["repo"], row["pr"], row["head"], row["seat"], head["ref"], row['id'],
            str(self.db), row['generation'], ci_fix=True))

    def _prepare_verdict_launch(self, turn: _Turn, head: dict) -> _Launch | None:
        """A review, a fix answering changes requested, or a ruling: the seat sees the PR's
        record (and, reviewer and fixer, the change itself)."""
        from .. import broker_ipc, gh
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
            # Same live checks as the claim, repeated right before launch: the claim's
            # reads may be minutes old, and a ruling on a moved or approved head is noise.
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
                reviews = [r for r in reviews
                           if not util.epoch(r.get('submitted_at')) > row['created']]
        # The reviewer and fixer see the change itself (#50); the adjudicator needs both
        # sides' comments. A read that failed is transient (retry); a moved head is not.
        # The shadow is prompted as the reviewer it shadows.
        prompt_row = dict(row, seat='reviewer') if row['seat'] == run_supervisor.SHADOW else row
        try:
            # The last allowed attempt degrades an unreadable file list rather than failing
            # the run (#110): a partial view, stated as such, instead of no turn at all.
            final = (row['retries'] or 0) + 1 >= run_supervisor.MAX_RETRIES
            change = (pr_facts.pr_change(loop, prompt_row, final=final)
                      if prompt_row['seat'] in ('reviewer', 'fixer') else None)
            # A shadow escalation's ruling answers the shadow review read HERE (#1112): the
            # prompt shows it and the scope records its run id, from one read, so a later
            # review landing during the turn is never taken as shown.
            shadow, extra = None, {}
            if (row['seat'] == 'adjudicator'
                    and ((marker or {}).get('escalated') or {}).get('by') == 'shadow'):
                shadow = turn_prompts.newest_shadow_review(row)
                extra = {'shadow': shadow}
            prompt = turn_prompts.isolated_prompt(loop, prompt_row, reviews, marker, change,
                                                  **extra)
        except ValueError as exc:
            if str(exc) in ('fixer answers unreadable', 'PR unreadable') \
                    or str(exc).startswith('PR files unreadable'):
                raise run_supervisor.RetryableError(str(exc)) from None
            raise
        # Host-owned, before launch (#93, #110): whether this seat sees the whole change, in
        # the ledger (explain, the receipt claim) and in the scope the broker is built from.
        # Nothing inside the namespace can reach either.
        partial = change.partial if change else ''
        self.record_view(turn.run_id, turn.owner, partial)
        scope = broker_ipc.RunScope(row["repo"], row["pr"], row["head"],
                                    prompt_row["seat"], head["ref"], row['id'],
                                    str(self.db), row['generation'], partial_view=partial,
                                    shadow_run=((shadow[0] if shadow else '')
                                                if row['seat'] == 'adjudicator' else None))
        if row['seat'] == 'adjudicator':
            from .. import state as state_mod
            # Last step before launch: mark the breach as being ruled on. Anyone else's
            # claim (a legacy gateway route included) refuses this one.
            if state_mod.state_for(loop).breach_start(row['pr'], row['head'],
                                                     marker['rounds']) is None:
                raise ValueError('breach marker already claimed or replaced')
            turn.breach = (state_mod.state_for(loop), marker['rounds'])
        return _Launch(prompt, scope, change=change)

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
        if shadow:
            # A shadow is a sample, never stuck work: a failed one is not retried (#218: the
            # first live one spent four whole turns' tokens to record nothing). A closed usage
            # window still waits for its reset, which costs nothing.
            turn.retry = False
            # What the shadow would have submitted (#218), recorded while the run is still live.
            submissions = observed.get('submissions') or []
            try:
                if submissions and self.record_shadow(run_id, owner, submissions[-1]):
                    self._shadow_escalation(turn)
                    if turn.loop is not None:              # #1163: the shadow's verdict is in
                        from .. import merge_check
                        merge_check.refresh_quietly(turn.loop, turn.row['pr'])
            except Exception:
                pass    # a lost record must not change how the turn ends
        state = self.complete_uncertain(
            run_id, owner, turn.rc, turn.error, stopped=turn.stopped, retry=turn.retry,
            detail=run_supervisor.output_detail(observed.get('stdout'), observed.get('stderr')),
            paced_until=turn.paced_until, cancel=turn.cancel)
        if turn.breach is not None and state in ('waiting', 'failed'):
            # This run marked the breach 'adjudicating' and made no ruling: hand the
            # marker back so its retry (or a re-arm) can claim it again.
            try:
                with self._connect() as con:
                    unwritten = run_supervisor.write_records(con, run_id) is None
                if unwritten:
                    turn.breach[0].breach_resume(turn.row['pr'], turn.row['head'], turn.breach[1])
            except Exception:
                pass
        run_supervisor.release_seat(turn.claim, state)
        if not shadow:
            # A shadow turn is no part of the loop (#218): its failure pages nobody.
            self.failure_notice(run_id, state)
        self.recover()
