"""The shadow review's turn steps (#218, #1004, #1361), moved out of ``turns`` at its size limit.

``ShadowTurns`` is a mixin like ``turns.ClaimAndLaunch``: ``Supervisor`` inherits both, so
``Supervisor(db)._shadow_ci_gate(turn)`` and a test's ``patch.object(Supervisor, ...)`` work as
before, with one binding. The steps are the shadow's claim read (``_claim_read_shadow``), its CI
gate, the record of its verdict with the escalation after it, and the launch of the fixer's answer
to an upheld shadow review. ``ClaimAndLaunch`` calls them as ``self.<step>``; they use the package's
names through the package (``run_supervisor.X``), so a test patching ``run_supervisor.X`` still
reaches them.
"""
from __future__ import annotations

import time

from .. import run_supervisor, util
from . import pr_facts, turn_prompts
from .turns import _ClaimRead, _Launch, _Turn


def _verdict_in(turn: _Turn, seat: str) -> bool:
    """#1361: a shadow turn whose last submission the broker authorized has done its job,
    however its process then exited (Hermes exits 1 for a turn that ends partial, at its step
    limit, say)."""
    submissions = turn.observed.get('submissions') or []
    return (seat == run_supervisor.SHADOW and bool(submissions)
            and isinstance(submissions[-1], dict) and bool(submissions[-1].get('authorized')))


class ShadowTurns:
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

    def _shadow_verdict_recorded(self, turn: _Turn) -> bool:
        """Record what a shadow turn would have submitted (#218), once, while the run is still
        live, and escalate a dissent. Answers whether a verdict the broker authorized is now
        recorded (#1361): pacing asks first, so a turn whose write failed is paced as before."""
        if turn.recorded is None:
            turn.recorded = False
            submissions = turn.observed.get('submissions') or []
            try:
                if submissions and self.record_shadow(turn.run_id, turn.owner, submissions[-1]):
                    turn.recorded = True
                    self._shadow_escalation(turn)
            except Exception:
                pass    # a lost record must not change how the turn ends
        return turn.recorded and _verdict_in(turn, run_supervisor.SHADOW)

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
        outcome = gate.shadows.shadow_escalation(loop, st, row['pr'], row['head'], reviews,
                                                 db=self.db)
        if outcome == "new":
            util.log(f"#{row['pr']} @ {row['head'][:7]} shadow review disagrees with the "
                     "approval: sent for a ruling")

    def _shadow_ci_gate(self, turn: _Turn) -> None:
        """A shadow review follows the reviewer's CI rules, strictly (#218): it waits, nothing
        spent, while the PR conflicts or a required check is running, cancelled, missing or
        unreadable; and it never reviews a red head, which it gives up (cancelled). Stricter
        than the reviewer on purpose: running checks hold it whatever ``review_after_ci`` says,
        a red head is never reviewed (the reviewer's waits for green or a new head, #1110,
        except where CI fixes are off for a fixer's PR), and past
        ``run_supervisor.CI_WAIT_MAX_S`` with CI unfinished it gives the head up rather than
        review it. A shadow verdict is a sample of a model, so it is taken only on code CI
        passed. Sets ``turn.error``."""
        from .. import ci, config, gh, shadow_queue
        row, loop = turn.row, turn.loop
        head = row['head']
        # #1647: only once its own reviewer turn approved the head; from the ledger, no read.
        # It holds for as long as that turn is unresolved, however long: a time bound would
        # end it with the head clear, and a late APPROVE would then be green with no shadow.
        what, why, approved = shadow_queue.decide(self.db, row)
        if what == "skip":
            turn.cancel = True
            turn.error = f"shadow review skipped: {why}"[:600]
            return
        if what == "wait":
            turn.paced_until = time.time() + run_supervisor.CI_POLL_S
            turn.error = f"{run_supervisor.VERDICT_HOLD} at {head[:7]} — {why}"[:600]
            self.held_for_ci = True
            return

        # Review of #1656, F11: the CI wait's clock starts at the approval waited for above, read
        # with it, not at the queueing, or a slow verdict would spend it before CI is first read.
        since = max(float(row['created']), approved or 0)

        def hold(why: str) -> None:
            if time.time() - since >= run_supervisor.CI_WAIT_MAX_S:
                turn.cancel = True
                turn.error = (f"shadow review skipped: CI did not finish at {head[:7]} within "
                              f"{run_supervisor.CI_WAIT_MAX_S // 60} min ({why})")[:600]
                return
            turn.paced_until = time.time() + run_supervisor.CI_POLL_S
            turn.error = run_supervisor.ci_hold_line(head, why)
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
                turn.error = run_supervisor.ci_hold_line(head, run_supervisor.CI_DRAFT_WAIT)
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

    def _prepare_shadow_fix_launch(self, turn: _Turn, head: dict) -> _Launch | None:
        """#1004 part 3: the fixer answers a shadow review the adjudicator upheld. The ruling is
        re-read now (a moved head or a later ruling ends the run), and the push is allowed only
        while it still stands (``RunScope.shadow_fix``, checked by the broker)."""
        from .. import gate, gh, prompts
        from ..broker import ipc as broker_ipc
        row, loop = turn.row, turn.loop
        ruled = gate.shadows.upheld_shadow_ruling(loop, row['pr'], row['head'], db=self.db)
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
            base = turn_prompts.isolated_prompt(loop, row, reviews, None, change)
            prompt = prompts.Prompt(base + turn_prompts.upheld_shadow_section(row, ruled),
                                    base.round_at)   # appended last: the offset still holds
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
