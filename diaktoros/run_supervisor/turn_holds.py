"""The claim's launch holds, split out of ``turns`` (which sits at the 1,500 size ceiling): the
pacing hold (#219, #457, #480) and the CI hold with its light poll (#241, #363, #539, #1110, #1648,
#1708).

``TurnHolds`` is a mixin of ``turns.ClaimAndLaunch``, so ``Supervisor`` inherits it and a test's
``patch.object(Supervisor, ...)`` works as before, with one binding. ``turns``' own helpers
(``_cap_held``, ``_ci_hold_text``, ``ci_wait_expired``, the hold-read checks, ``_ClaimRead``) are
read through that module at call time, and the package's names through the package
(``run_supervisor.X``), so a patch on either still reaches this code.
"""
from __future__ import annotations

import time

from .. import run_supervisor, util
from . import turns as _turns


class TurnHolds:
    """The pacing and CI holds of :class:`~diaktoros.run_supervisor.turns.ClaimAndLaunch`."""

    def _pacing_hold(self, turn: _turns._Turn, provider: str, account: str) -> bool:
        """Pacing (#219): an account whose usage window is closed, or a seat past its daily
        cap, waits for the reset instead of launching a turn that would only 429. Sets
        ``turn.error`` when held; answers whether this is a review-only turn."""
        from .. import config, gh, pacing
        row, loop = turn.row, turn.loop
        closed = pacing.held(account)
        if closed is not None:
            turn.paced_until = closed[0]
            turn.error = pacing.hold_line(row['seat'], provider, closed[0], closed[1])
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
                _turns._cap_held(turn, pacing.REVIEW_ONLY_SEAT, ro_cap)
                return ro_turn
        else:
            cap = config.seat_daily_turns(loop, row['seat'])
            if cap is not None and pacing.turns_today(loop['id'], row['seat']) >= cap:
                _turns._cap_held(turn, row['seat'], cap)
                return False
        # A seat's daily token budget (#480): once spent, the next turn waits for midnight.
        # A turn in flight is never killed; this only gates a launch. The fixer's budget
        # covers its issue fixes too (seat_daily_tokens, and record_usage's counter key).
        budget_seat = 'fixer' if row['seat'] == 'issue_fixer' else row['seat']
        token_cap = config.seat_daily_tokens(loop, row['seat'])
        if token_cap is not None and pacing.tokens_today(loop['id'], budget_seat) >= token_cap:
            turn.paced_until = pacing.next_midnight()
            turn.error = run_supervisor.daily_hold_line(
                row['seat'], run_supervisor.DAILY_TOKEN_BUDGET, token_cap,
                pacing.when(turn.paced_until))
            return False
        return ro_turn

    def _ci_hold(self, turn: _turns._Turn, expired: bool = False) -> None:
        """A reviewer turn held for CI, nothing spent (sets ``turn.error``). The decision is
        ``_ci_hold_reason``'s, which the claim's light poll (``_ci_poll``) makes too."""
        from .. import gh
        row, loop = turn.row, turn.loop
        queued = gh.api(loop, f'/repos/{row["repo"]}/pulls/{row["pr"]}',
                        login=loop["read_token"])
        reason = self._ci_hold_reason(row, loop, queued, expired)
        if reason:
            # Nothing spent: no model call, no daily turn, no retry. This worker stays
            # to re-queue it (linger_for_ci); the watchdog sweep is the backstop.
            turn.paced_until = time.time() + run_supervisor.CI_POLL_S
            turn.error = reason
            self.held_for_ci = True

    def _ci_hold_reason(self, row, loop: dict, queued, expired: bool = False) -> str | None:
        """Why a reviewer row at ``row['head']`` is held for CI, or None when it is not.
        ``queued`` is the PR as just read.

        A cancelled check (#363) holds every review: the broker would refuse its APPROVE, so
        the turn could only spend tokens. Running checks hold it only with review_after_ci
        (#241), even beside a failed one. A failed check holds while the fixer takes it first
        (#539); then, and on a review-only PR, which no fixer pushes to, it holds until the
        checks pass (a rerun) or a new head supersedes it (#1110): a verdict that only says
        "CI failed" would spend a round and close the head to re-review. Only where CI fixes
        are off for a fixer's PR does the review run, since it is how the fixer hears of the
        failure. ``expired``: past ``run_supervisor.CI_WAIT_MAX_S`` only that red hold remains."""
        from .. import ci, ci_fix, config
        # A PR that conflicts with its base gets no pull_request CI from GitHub, so the
        # required checks could never report: hold, nothing spent. The merge push makes
        # a new head that supersedes this row. unknown/null mergeability is no conflict.
        # A closed or draft PR is left to the eligibility checks, which supersede it.
        if not expired and run_supervisor._conflicts_at_head(queued, row["head"]):
            base = queued.get("base")
            base = str(base.get("ref") or "") if isinstance(base, dict) else ""
            return run_supervisor.ci_hold_line(
                row['head'], f"conflicts with {base or 'its base'} — GitHub runs no CI on it")
        author = (str(((queued.get("user") or {}).get("login")) or "")
                  if isinstance(queued, dict) else "")
        review_only = author.lower() in config.review_only(loop)
        # Red CI no review answers: no fixer pushes here, or the fixer's CI fixes are done.
        holdable = review_only or ci_fix.fixable(loop, author)
        if expired and not holdable:
            return None                 # past the wait, only that red hold remains
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
            return None
        # #374: a failure releases the cancelled/missing holds only (the review would
        # otherwise go unreported); the pending wait survives it, so one review names
        # every failure once CI finishes.
        if checks is not None and (
                fixing or red
                or (not checks.failed and (checks.cancelled or checks.missing))
                or (after_ci and checks.pending)):
            return _turns._ci_hold_text(row['head'], checks, fixing, red, review_only)
        return None

    def _ci_poll(self, row) -> bool:
        """The claim's cheap path for a reviewer row held for CI (#1648): re-read only what
        decides the hold, the PR (still open, ready and at this head?) and the head's
        check-runs and statuses, not the whole claim read set every ``CI_POLL_S``.

        True when the row is still held: it waits again and nothing else was read. False sends
        it through the full claim, once: CI settled, the head moved, the PR closed or went
        draft, or a read failed. A moved head is then superseded by the claim's own read, as
        it always was."""
        if (row['seat'] != 'reviewer'
                or not str(row['error'] or '').startswith(run_supervisor.CI_HOLD)):
            return False
        from .. import config, gh
        try:
            loop = config.by_repo(row['repo'])
            if loop is not None and self._ci_poll_defers(row, loop):
                return False
            queued = (gh.api(loop, f"/repos/{row['repo']}/pulls/{row['pr']}",
                             login=loop['read_token']) if loop is not None else None)
            if (not isinstance(queued, dict) or queued.get('number') != row['pr']
                    or queued.get('state') != 'open' or queued.get('draft') is not False
                    or (queued.get('head') or {}).get('sha') != row['head']):
                return False
            reason = self._ci_hold_reason(row, loop, queued, _turns.ci_wait_expired(row))
        except Exception:
            return False                # an unreadable poll is the full claim's to settle
        if not reason:
            return False
        now = time.time()
        with self._connect() as con:
            changed = con.execute("UPDATE runs SET state='waiting', retry_at=?, error=?, updated=? "
                                  "WHERE id=? AND state='pending' AND owner IS NULL",
                                  (now + run_supervisor.CI_POLL_S, reason, now, row['id'])).rowcount
        if changed:
            self.held_for_ci = True
            self.failure_notice(row['id'], 'waiting')   # once per kind, as a held turn's is
            self._refresh_check(loop, row, 'waiting')   # a changed reason shows (#2326)
        return True

    def _ci_poll_defers(self, row, loop: dict) -> bool:
        """Whether a held-for-CI row must take the full claim now, whatever CI says (#1708):
        its head is escalated for a ruling (the claim's own ``_claim_read_escalated``, which
        then retires it), or the watchdog sweep has recorded a maintainer's hold on the PR
        (``pr_holds``, #477), which the claim's live ``pr_hold.read`` then decides. Both are
        local reads, so the light poll stays light. The sweep's record is a cache that can lag
        (no sweep yet, a failed read, a first sweep that stops before the hold scan), so once
        every ``run_supervisor.CI_HOLD_READ_S`` of the hold the poll also asks GitHub itself
        (``_ci_poll_hold_due``, two calls): a hold the sweep has not recorded is seen within
        that bound, so a red hold with no time limit cannot hide one indefinitely."""
        from .. import state as state_mod
        read = _turns._ClaimRead()
        self._claim_read_escalated(row, read)
        if read.superseded or read.read_error:
            return True
        st = state_mod.state_for(loop)
        if row['pr'] in st.pr_holds():
            return True
        return _turns._ci_poll_hold_due(row, st) and _turns._ci_poll_live_hold(row, loop)
