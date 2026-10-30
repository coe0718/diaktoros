"""A run's intake and the operator's re-arm and stop (#1349, moved out of the core):
``enqueue`` and ``submit`` (a delivery becomes one pending run, deduplicated), ``retry`` with
``_rearm``, and ``cancel`` / ``cancel_requested``.

``RunIntake`` is a mixin: ``Supervisor`` inherits it, so ``Supervisor(db).submit(...)`` and a
test's ``patch.object(Supervisor, ...)`` work as before, with one binding. It uses the
``_connect()`` ``Supervisor`` provides, and the package's names through the package
(``run_supervisor.X``), so a test patching ``run_supervisor.X`` still reaches this code.
"""
from __future__ import annotations

import time
import uuid
from contextlib import nullcontext

from .. import run_supervisor


class RunIntake:
    """The intake, retry and cancel methods of :class:`~diaktoros.run_supervisor.Supervisor`."""

    def enqueue(self, delivery: str, repo: str, pr: int, head: str, seat: str,
                *, turn_key: str = '', require_push_admission: bool = False,
                budget: float | None = None) -> str:
        """Commit identity before any spawn. A repeated delivery cannot change terms."""
        self.submit(delivery, repo, pr, head, seat, turn_key=turn_key,
                    require_push_admission=require_push_admission, budget=budget)
        return run_supervisor.SILENT

    def submit(self, delivery: str, repo: str, pr: int, head: str, seat: str,
               *, turn_key: str = '', require_push_admission: bool = False,
               budget: float | None = None) -> str:
        """``enqueue``, reporting what it did (issue #73): ``enqueued`` (new row),
        ``rearmed`` (a failed/cancelled pre-write run is pending again), ``pending``
        (an unclaimed row, worker re-armed), or ``duplicate <state>[: why]`` — nothing
        was scheduled, and the caller must not report a fresh enqueue.

        ``require_push_admission`` (the fixer gate's path): a production fixer turn that the
        host policy would not admit is refused with ``FixerPushDisabled`` *before* any row is
        written, under the same policy lock as the admission snapshot. The verdict is then
        held by the gate, and a later opt-in admits a fresh row instead of an old one.

        ``budget`` is this turn's wall clock in seconds (the loop's per-seat ``turn_budget_s``,
        #49), recorded on the row so whichever worker claims it runs it on the loop's terms;
        ``None`` means this supervisor's ``child_timeout``. A re-arm by a new event takes the
        budget the loop has *now*: a turn killed at its budget reruns on the raised one.
        """
        requested = budget
        if budget is None:
            budget = self.child_timeout
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not budget > 0:
            raise ValueError("positive turn budget required")
        if not all(isinstance(v, str) and v and len(v) <= 256 for v in
                   (delivery, repo, head, seat)) or not isinstance(turn_key, str) or len(turn_key) > 256 or type(pr) is not int or pr <= 0:
            raise ValueError("invalid run identity")
        if seat not in self.capacity:
            raise ValueError("unconfigured seat")
        now = time.time()
        # Serialize policy snapshot and durable enqueue with explicit toggles.
        # Re-delivery of an old row never upgrades its admission.
        from .. import config
        lock = config.push_policy_lock() if self.production_config and seat == 'fixer' else nullcontext()
        with lock, self._connect() as con:
            admitted = 0
            if self.production_config and seat == 'fixer':
                loop = config.by_repo(repo)
                if loop is not None and config.unattended_fixer_push_enabled(loop):
                    admitted = 1
            con.execute("BEGIN IMMEDIATE")
            prior = con.execute("SELECT * FROM runs WHERE delivery=?", (delivery,)).fetchone()
            if prior:
                if (prior["repo"], prior["pr"], prior["head"], prior["seat"], prior['turn_key']) != (repo, pr, head, seat, turn_key):
                    raise ValueError("delivery identity collision")
            else:
                prior = con.execute("SELECT * FROM runs WHERE repo=? AND pr=? AND head=? AND seat=? AND turn_key=?",
                                    (repo, pr, head, seat, turn_key)).fetchone()
                if not prior and require_push_admission and self.production_config \
                        and seat == 'fixer' and not admitted:
                    con.execute("ROLLBACK")
                    raise run_supervisor.FixerPushDisabled(repo)
                if not prior:
                    con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,turn_key,state,created,updated,push_admitted,budget) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                (uuid.uuid4().hex, delivery, repo, pr, head, seat, turn_key,
                                 "pending" if self.fixture_mode or self.production_config else "blocked", now, now, admitted,
                                 float(budget)))
            outcome = 'enqueued' if not prior else 'pending' if prior['state'] == 'pending' \
                else f"duplicate {prior['state']}"
            if prior and prior['state'] in run_supervisor.REARMABLE and (self.fixture_mode or self.production_config):
                # A new event for a head whose run never wrote is a reason to try again
                # (#53): re-arm it, one attempt per event, up to MAX_REARMS failures.
                wrote = run_supervisor.write_evidence(con, prior['id'])
                if wrote is not None:
                    outcome += f': {wrote} — reconcile, never replayed'
                elif prior['state'] == 'cancelled' and run_supervisor.operator_cancelled(prior['error']):
                    # An operator stopped it (#1148): an event never undoes that.
                    outcome += (": cancelled by an operator — only `hermes dk retry` re-arms it; "
                                "a new head queues a fresh run")
                elif prior['error'] == run_supervisor.FIXER_NOT_ADMITTED and prior['push_admitted'] != 1:
                    # Never upgraded by an event: re-arming would only be cancelled again.
                    outcome += (": fixer push not admitted — a redelivered event never upgrades "
                                "admission; after opting in, `hermes dk retry` "
                                "re-admits it")
                elif (prior['error'] == run_supervisor.FIXER_PUSH_REVOKED and self.production_config
                      and seat == 'fixer' and not admitted):
                    # Pushes are still off (``admitted`` is this event's fresh policy read, taken
                    # under the push-policy lock): the claim would cancel it again. Once they are
                    # back on, an event re-arms it.
                    outcome += (": fixer push revoked — unattended fixer pushes are still off, so "
                                "a re-armed run would only be cancelled again; turn them on, then "
                                "a redelivered event or `hermes dk retry` re-arms it")
                elif (prior['retries'] or 0) >= run_supervisor.MAX_REARMS:
                    outcome += (f": {prior['retries']} failed attempts — only "
                                "`hermes dk retry` re-arms it")
                else:
                    self._rearm(con, prior['id'], reset=False, budget=requested)
                    outcome = 'rearmed'
            elif prior and prior['state'] == 'waiting' and prior['retry_at']:
                outcome += f" (retry {prior['retries']} due in {max(0, int(prior['retry_at'] - now))}s)"
            con.execute("COMMIT")
        # A redelivery of an unclaimed run must rearm the worker after a
        # transient generation/read outage; active or completed runs stay deduped.
        if (self.fixture_mode or self.production_config) and outcome in ('enqueued', 'pending', 'rearmed'):
            self._spawn()
        return outcome

    @staticmethod
    def _rearm(con, run_id: str, *, reset: bool, budget: float | None = None) -> None:
        """Make a pre-write run pending again, inside the caller's transaction. Admission,
        generation terms and history stay; claim attempts restart; its notice is resolved
        so a later failure is reported afresh. ``budget`` (the loop's current seat budget,
        #49) replaces the row's, so a turn killed at its budget reruns on the raised one."""
        if budget is not None:
            con.execute("UPDATE runs SET budget=? WHERE id=?", (float(budget), run_id))
        con.execute("UPDATE runs SET state='pending', owner=NULL, lease=NULL, pid=NULL, "
                    "launch_intent=NULL, outcome=NULL, attempts=0, retry_at=NULL, "
                    "cancel_requested=NULL, "
                    "retries=CASE WHEN ? THEN 0 ELSE retries END, updated=? WHERE id=?",
                    (1 if reset else 0, time.time(), run_id))
        con.execute("DELETE FROM operator_notices WHERE run_id=?", (run_id,))

    def retry(self, run_id: str, budget: float | None = None) -> str:
        """Operator re-arm of a failed or waiting run that never wrote, of a fixer run the
        push policy cancelled at claim (#53; ``policy_cancelled``), or of a run an operator
        cancelled (#1148). Any other cancellation is superseded (head moved, PR closed) and
        refused: a new head gets its own turn.

        ``budget``, when given, is the loop's seat budget now (#49): a turn killed at its
        budget is re-armed on the raised one, not the budget recorded when it was enqueued.

        Refuses anything that may have written — uncertain, quarantined, reconciled, or with
        a receipt claim, push intent or ruling on record — with the reconcile instructions.
        Resets the automatic retry budget. Returns the new state; raises ValueError on refusal.

        A fixer run is re-admitted under the push policy in force *now*: a fresh admission
        snapshot taken under the push-policy lock, the same lock ``submit`` and the broker's push
        hold. While unattended fixer pushes are off the retry is refused with the command that
        turns them on. (A redelivered event never upgrades admission; only this does.)
        """
        from .. import config
        with self._connect() as con:
            first = con.execute('SELECT repo,seat,pr,head FROM runs WHERE id=?',
                                (run_id,)).fetchone()
        fixer = first is not None and first['seat'] == 'fixer'
        st = None
        if first is not None and first['seat'] == run_supervisor.SHADOW:
            # A shadow is judged on the findings snapshot taken when it was queued (#1013), and
            # one that has ended may have had it evicted. Hold the state lock from the check to
            # the re-arm (state lock, then ledger: the order enqueue_shadow uses), so a put
            # cannot evict the snapshot in between.
            from .. import state as state_mod
            try:
                loop = config.by_repo(first['repo'])
                st = state_mod.state_for(loop) if loop is not None else None
            except config.ConfigError:
                st = None
        # An issue fix publishes under the same push policy (and triage and a fix label, #980).
        issue_fix = first is not None and first['seat'] == 'issue_fixer'
        shadow = first is not None and first['seat'] == run_supervisor.SHADOW
        with (st.locked() if st is not None else nullcontext()), \
                (config.push_policy_lock() if fixer or issue_fix else nullcontext()), \
                self._connect() as con:
            admitted, policy_off = None, ''
            if shadow and (st is None
                           or st.shadow_findings_get(first['pr'], first['head']) is None):
                policy_off = ("refused: the findings snapshot this shadow was queued with is "
                              "gone (evicted, or never taken), so it would be judged on the live "
                              "findings ledger, which the real review may have changed (#1013); "
                              "a new head gets a fresh shadow")
            if issue_fix:
                try:
                    loop = config.by_repo(first['repo'])
                    reason = (run_supervisor.issue_fix_off_reason(loop) if loop is not None
                              else f"no loop is configured for {first['repo']}")
                except config.ConfigError as exc:
                    reason = f"the loop configuration for {first['repo']} is unusable ({exc})"
                if reason:
                    policy_off = (f"refused: {reason}, so an issue fix could not publish; turn "
                                  "it on (triage with a fix label, and unattended fixer "
                                  "pushes), then retry")
            if fixer:
                try:
                    loop = config.by_repo(first['repo'])
                except config.ConfigError as exc:
                    # Not a ValueError: named here so `retry` reports it per run, not a crash.
                    loop, policy_off = None, (f"refused: the loop configuration for "
                                              f"{first['repo']} is unusable ({exc})")
                if not policy_off and (loop is None
                                       or not config.unattended_fixer_push_enabled(loop)):
                    command = config.fixer_push_enable_command(loop or "LOOP")
                    policy_off = (f"refused: unattended fixer pushes are off for "
                                  f"{first['repo']}, so a fixer turn could not publish; run "
                                  f"`{command}` first, then retry")
                admitted = 1
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT state,error FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                con.execute('COMMIT')
                raise ValueError(f'no run {run_id}')
            wrote = run_supervisor.write_evidence(con, run_id)
            if wrote is not None:
                con.execute('COMMIT')
                raise ValueError(
                    f'refused: {wrote}. A run that may have written is never replayed. Inspect '
                    'the PR for its external writes, establish no worker remains, then '
                    f'`python -m diaktoros.run_supervisor reconcile DB {run_id} --reason '
                    "'external writes inspected' --acknowledge-no-live-worker`; a new head "
                    'gets a fresh turn.')
            if row['state'] not in run_supervisor.REARMABLE + ('waiting',):
                con.execute('COMMIT')
                raise ValueError(f"refused: run is {row['state']}, not failed, waiting or "
                                 "cancelled")
            if row['state'] == 'cancelled' and not (run_supervisor.policy_cancelled(row['error'])
                                                    or run_supervisor.operator_cancelled(row['error'])):
                # Superseded (head moved, PR closed): re-arming it would only be cancelled again
                # by the claim; a new head gets its own turn (the same line runs_view draws).
                con.execute('COMMIT')
                raise ValueError(f"refused: cancelled because {row['error'] or 'superseded'} — "
                                 "a new head gets its own turn; nothing to retry")
            if policy_off:
                # After the write and state checks: those refusals say more about the run.
                con.execute('COMMIT')
                raise ValueError(policy_off)
            if budget is not None and (isinstance(budget, bool) or not budget > 0):
                con.execute('COMMIT')
                raise ValueError('positive turn budget required')
            if admitted is not None:
                con.execute('UPDATE runs SET push_admitted=? WHERE id=?', (admitted, run_id))
            self._rearm(con, run_id, reset=True, budget=budget)
            con.execute('COMMIT')
        return 'pending'

    def cancel(self, run_id: str) -> dict:
        """An operator's cancel of one run (#1148). Returns ``{"was": state, "state": state}``;
        raises ValueError naming the state of a run that has already finished.

        A run that has not started (``CANCEL_AT_ONCE``) is cancelled now, in this transaction:
        ``OPERATOR_CANCEL`` keeps every redelivery and sweep from re-arming it, and only
        ``retry`` does. A claimed run loses its owner, so its worker never launches it. A
        launching or running turn is the owning worker's to stop: the request is recorded
        (``cancel_requested``), the state is left alone, and the worker's heartbeat stops the
        sandbox through the turn budget's own kill, then ends the run as ``cancelled`` — or
        ``uncertain`` when it holds write-ahead evidence (``complete_uncertain``).
        """
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT state FROM runs WHERE id=?', (run_id,)).fetchone()
            if row is None:
                con.execute('COMMIT')
                raise ValueError(f'no run {run_id}')
            was, now = row['state'], time.time()
            if was in run_supervisor.CANCEL_AT_ONCE:
                # Pre-launch by construction; a write-ahead record here would be a ledger that
                # cannot be trusted, so it is quarantined instead of cleared.
                wrote = run_supervisor.write_records(con, run_id)
                state = 'uncertain' if wrote is not None else 'cancelled'
                error = (f"{run_supervisor.OPERATOR_CANCEL} after it may have written ({wrote}) — never replayed; "
                         "inspect and reconcile" if wrote is not None else run_supervisor.OPERATOR_CANCEL)
                con.execute("UPDATE runs SET state=?, error=?, owner=NULL, lease=NULL, "
                            "retry_at=NULL, updated=?, finished=? WHERE id=? AND state=?",
                            (state, error, now, now, run_id, was))
            elif was in run_supervisor.CANCEL_BY_WORKER:
                state = 'stopping'
                con.execute("UPDATE runs SET cancel_requested=COALESCE(cancel_requested, ?), "
                            "updated=? WHERE id=? AND state=?", (now, now, run_id, was))
            else:
                con.execute('COMMIT')
                raise ValueError(f"refused: run is {was} — it has already finished")
            con.execute('COMMIT')
        return {"was": was, "state": state}

    def cancel_requested(self, run_id: str, owner: str) -> bool:
        """Whether an operator asked to stop this owner's turn (#1148)."""
        with self._connect() as con:
            row = con.execute("SELECT cancel_requested FROM runs WHERE id=? AND owner=?",
                              (run_id, owner)).fetchone()
        return row is not None and row['cancel_requested'] is not None
