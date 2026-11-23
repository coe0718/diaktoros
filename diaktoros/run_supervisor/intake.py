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
from . import retry_terms, shadow_owing


def redelivery_refusal(con, prior, *, admitted: bool, production: bool, seat: str) -> str | None:
    """Why a new event for a ``REARMABLE`` run's head re-arms nothing, or None when it re-arms
    it (#53). The one rule ``submit`` applies and explain's next step reads (#1514 review): a
    redelivery is not ``retry``, which offers different rows (``retry_action.offered``).

    ``admitted`` is that event's own push-policy read, ``production`` whether a production
    configuration is in force; the row's state is the caller's to have checked."""
    wrote = run_supervisor.write_evidence(con, prior['id'])
    if wrote is not None:
        return f'{wrote} — reconcile, never replayed'
    if prior['state'] == 'cancelled' and run_supervisor.operator_cancelled(prior['error']):
        # An operator stopped it (#1148): an event never undoes that.
        return ("cancelled by an operator — only `hermes dk retry` re-arms it; "
                "a new head queues a fresh run")
    if prior['error'] == run_supervisor.FIXER_NOT_ADMITTED and prior['push_admitted'] != 1:
        # Never upgraded by an event: re-arming would only be cancelled again.
        return ("fixer push not admitted — a redelivered event never upgrades "
                "admission; after opting in, `hermes dk retry` "
                "re-admits it")
    if (prior['error'] == run_supervisor.FIXER_PUSH_REVOKED and production
            and seat == 'fixer' and not admitted):
        # Pushes are still off (``admitted`` is this event's fresh policy read, taken
        # under the push-policy lock): the claim would cancel it again. Once they are
        # back on, an event re-arms it.
        return ("fixer push revoked — unattended fixer pushes are still off, so "
                "a re-armed run would only be cancelled again; turn them on, then "
                "a redelivered event or `hermes dk retry` re-arms it")
    if (prior['retries'] or 0) >= run_supervisor.MAX_REARMS:
        return (f"{prior['retries']} failed attempts — only "
                "`hermes dk retry` re-arms it")
    return None


# Why a shadow run is not re-armed once its findings snapshot is gone (#1013, #1139). One text
# for ``retry`` and for ``gate.shadows.enqueue_shadow``, the two ways a shadow row can be re-armed.
SHADOW_SNAPSHOT_GONE = (
    "the findings snapshot this shadow was queued with is gone (evicted, or never taken), so it "
    "would be judged on the live findings ledger, which the real review may have changed "
    "(#1013); a new head gets a fresh shadow")


# Why a reviewer turn that owes a shadow was not re-armed or left to run (#2634 review F1).
SHADOW_OWED = "a shadow review is owed but could not be queued"


def shadow_owed_stop(error: object) -> bool:
    """Whether a run was stopped, or failed before it started, for the shadow its turn owes and
    could not have (#2634): not an operator's cancel, so an event or ``retry`` re-arms it."""
    return isinstance(error, str) and error.startswith(SHADOW_OWED)


def shadow_snapshot_refusal(st, run) -> str:
    """Why ``retry`` refuses a shadow run (a ledger row with ``pr``, ``head`` and ``turn_key``),
    or ``''``: the findings snapshot kept for its own reviewer turn is gone (#1013, #1139). ``st``
    is the loop's state, None when it cannot be read. Snapshots are kept per reviewer turn, so one
    another turn took at the same head, or one from before turns were keyed, does not count, as
    in ``gate.shadows.enqueue_shadow``."""
    if st is None or st.shadow_findings_get(run['pr'], run['head'], run['turn_key'] or '') is None:
        return f"refused: {SHADOW_SNAPSHOT_GONE}"
    return ''


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
               budget: float | None = None, shadow: tuple[str, float] | None = None,
               settle: float = 0.0) -> str:
        """``enqueue``, reporting what it did (issue #73): ``enqueued`` (new row),
        ``rearmed`` (a failed/cancelled pre-write run is pending again), ``pending``
        (an unclaimed row, worker re-armed), or ``duplicate <state>[: why]`` — nothing
        was scheduled: never report it as a fresh enqueue.

        ``require_push_admission`` (the fixer gate's path): a production fixer turn the host
        policy would not admit is refused with ``FixerPushDisabled`` *before* any row is
        written, under the same policy lock as the admission snapshot. The gate then holds the
        verdict, and a later opt-in admits a fresh row, never an old one.

        ``budget`` is this turn's wall clock in seconds (the loop's per-seat ``turn_budget_s``,
        #49), on the row so whichever worker claims it runs it on the loop's terms;
        ``None`` means this supervisor's ``child_timeout``. A re-arm by a new event takes the
        loop's budget *now*: a turn killed at its budget reruns on the raised one.

        ``shadow`` (a sampled reviewer turn's ``(delivery, budget)``, #1682): its shadow's row,
        written with this one (or ``shadow_owing``). ``settle``: a new row waits (#2248).
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
                    self._insert_runs(con, [(delivery, seat, admitted, budget)], shadow,
                                      (repo, pr, head, turn_key), now, settle)
            outcome = 'enqueued' if not prior else 'pending' if prior['state'] == 'pending' \
                else f"duplicate {prior['state']}"
            with shadow_owing.fails_closed(self, con, prior, shadow):
                if prior:
                    outcome = self._redeliver(con, prior, outcome, bool(admitted), requested,
                                              settle, shadow, now)
                con.execute("COMMIT")
        # A redelivery of an unclaimed run must rearm the worker after a
        # transient generation/read outage; active or completed runs stay deduped.
        self._start_worker(repo, prior, outcome, settle)
        return outcome

    def _redeliver(self, con, prior, outcome: str, admitted: bool, budget, settle: float,
                   shadow, now: float) -> str:
        """``submit``'s answer for a row already at the delivery. A ``REARMABLE`` row: a new event
        for a head whose run never wrote is a reason to try again (#53), one attempt per event,
        up to MAX_REARMS failures. A re-armed reviewer turn owes its shadow too (#2072, #2076):
        one from before #1682, or first queued unsampled, may have no shadow row, so it is
        written in this transaction. So does one the event finds not ended, whatever a worker did
        with it meanwhile (#2627, #2634 review F4): ``shadow_owing.beside``.

        ``shadow`` as a ``str`` (#2634 review F1): owed, but its findings snapshot could not be
        kept. Unknown is never "none owed": a failed row is not re-armed, and one not ended is
        failed or stopped (``beside``); the gate holds the delivery on the outcome naming
        ``SHADOW_OWED``, and a later event or ``retry`` re-arms it with its shadow."""
        unkept = f"{SHADOW_OWED}: {shadow}" if isinstance(shadow, str) else ''
        if prior['state'] in shadow_owing.UNENDED:
            return shadow_owing.beside(self, con, prior, outcome, shadow, unkept, now)
        if prior['state'] not in run_supervisor.REARMABLE or not (self.fixture_mode
                                                                  or self.production_config):
            return outcome
        refused = redelivery_refusal(con, prior, admitted=admitted,
                                     production=bool(self.production_config), seat=prior['seat'])
        if refused is not None or unkept:
            return f"{outcome}: {refused or unkept}"
        self._rearm(con, prior['id'], reset=False, budget=budget, settle=settle)
        self._insert_runs(con, [], shadow, (prior['repo'], prior['pr'], prior['head'],
                                            prior['turn_key']), now)
        return 'rearmed'

    def _start_worker(self, repo: str, prior, outcome: str, settle: float) -> None:
        """Spawn ``submit``'s worker, if it wants one; for a settling row, one told its repo, so
        it lingers for that loop's settling rows only, never another's (#2569)."""
        if not self._wants_worker(prior, outcome):
            return
        settling = (settle > 0 and outcome in ('enqueued', 'rearmed')) or (
            prior is not None and prior['state'] == 'waiting'
            and prior['error'] == run_supervisor.SETTLE_HOLD)
        if not settling:
            self._spawn()
            return
        self.settle_repos = self.settle_repos | {repo}
        self._spawn(settle_repo=repo)

    def _wants_worker(self, prior, outcome: str) -> bool:
        """Whether ``submit`` spawns a worker: for a run it made runnable, and for a redelivery
        of a reply's row still settling (#2248 review F2), so a lost spawn is not left to an
        unrelated recover to start once due. A worker lingers until that row is due."""
        if not (self.fixture_mode or self.production_config):
            return False
        return outcome in ('enqueued', 'pending', 'rearmed') or (
            prior is not None and prior['state'] == 'waiting'
            and prior['error'] == run_supervisor.SETTLE_HOLD)

    def _insert_runs(self, con, rows: list, shadow, identity: tuple, now: float,
                     settle: float = 0.0) -> None:
        """Insert new run rows, ``(delivery, seat, push_admitted, budget)`` each, in the caller's
        transaction. With ``shadow``, a reviewer turn's shadow row goes in with it (#1682), and
        with a re-armed one or one still to start (``rows`` empty, #2076, #2627, #2628): no new,
        re-armed or redelivered reviewer row is visible without the shadow it owes (written
        ``pending`` as ``retry``'s re-arm is, even here with no launcher), so neither an APPROVE posted
        before a second submit nor that submit failing can leave ``shadow.escalate`` with
        nothing to wait for. A failure rolls both back. A shadow row already at the identity
        is left as it is, for ``gate.shadows.enqueue_shadow`` to re-arm or refuse.

        ``settle`` seconds (#2248): the turn's own row is written ``waiting`` on ``SETTLE_HOLD``
        until then, not runnable now; ``recover`` makes it pending once due, and its claim and
        pre-launch reads decide it as any pending row's."""
        repo, pr, head, turn_key = identity
        if isinstance(shadow, str):
            # A new reviewer row that owes a shadow it cannot have is not written (#2634 F3).
            raise RuntimeError(f"reviewer turn not armed: {SHADOW_OWED}: {shadow}")
        state = "pending" if self.fixture_mode or self.production_config or not rows else "blocked"
        if isinstance(shadow, tuple) and not con.execute(
                "SELECT 1 FROM runs WHERE delivery=? OR (repo=? AND pr=? AND head=? AND "
                "seat=? AND turn_key=?)", (shadow[0], repo, pr, head, run_supervisor.SHADOW,
                                            turn_key)).fetchone():
            if run_supervisor.SHADOW not in self.capacity or not shadow[1] > 0:
                raise ValueError("a shadow row needs the shadow seat and a positive budget")
            rows = [*rows, (shadow[0], run_supervisor.SHADOW, 0, shadow[1])]
        for index, (delivery, seat, admitted, budget) in enumerate(rows):
            held = index == 0 and settle > 0 and state == "pending"
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,turn_key,state,created,"
                        "updated,push_admitted,budget,retry_at,error) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, delivery, repo, pr, head, seat, turn_key,
                         "waiting" if held else state, now, now, admitted, float(budget),
                         now + settle if held else None,
                         run_supervisor.SETTLE_HOLD if held else None))

    @staticmethod
    def _rearm(con, run_id: str, *, reset: bool, budget: float | None = None,
               settle: float = 0.0) -> None:
        """Make a pre-write run pending again, inside the caller's transaction. Admission,
        generation terms and history stay; claim attempts restart; its notice is resolved
        so a later failure is reported afresh. ``budget`` (the loop's current seat budget,
        #49) replaces the row's, so a turn killed at its budget reruns on the raised one.
        ``settle`` seconds (a reply's redelivery, #2248 review F3): it waits on ``SETTLE_HOLD``
        as a new row would, not pending at once."""
        if budget is not None:
            con.execute("UPDATE runs SET budget=? WHERE id=?", (float(budget), run_id))
        now, held = time.time(), settle > 0
        con.execute("UPDATE runs SET state=?, owner=NULL, lease=NULL, pid=NULL, "
                    "launch_intent=NULL, outcome=NULL, attempts=0, retry_at=?, "
                    "cancel_requested=NULL, error=CASE WHEN ? THEN ? ELSE error END, "
                    "retries=CASE WHEN ? THEN 0 ELSE retries END, updated=? WHERE id=?",
                    ("waiting" if held else "pending", now + settle if held else None,
                     1 if held else 0, run_supervisor.SETTLE_HOLD, 1 if reset else 0, now,
                     run_id))
        con.execute("DELETE FROM operator_notices WHERE run_id=?", (run_id,))

    def retry(self, run_id: str, budget: float | None = None) -> str:
        """Operator re-arm of a failed or waiting run that never wrote, of a fixer run the
        push policy cancelled at claim (#53; ``policy_cancelled``), or of a run an operator
        cancelled (#1148). Any other cancellation is superseded (head moved, PR closed) and
        refused: a new head gets its own turn.

        ``budget``, when given, is the loop's seat budget now (#49): a turn killed at its
        budget is re-armed on the raised one, not the budget recorded when it was enqueued.

        Refuses anything that may have written — uncertain, quarantined, reconciled, or with
        any write-ahead record (``write_records``) — with the reconcile instructions.
        Resets the automatic retry budget. Returns the new state; raises ValueError on refusal.

        A fixer run is re-admitted under the push policy in force *now*: a fresh admission
        snapshot taken under the push-policy lock, the same lock ``submit`` and the broker's push
        hold. While unattended fixer pushes are off the retry is refused with the command that
        turns them on. (A redelivered event never upgrades admission; only this does.)

        A reviewer run whose turn is sampled is re-armed with the shadow row it owes, on a
        findings snapshot kept now, in the same transaction (#2628, ``retry_terms``); one whose
        shadow cannot be queued, or whose loop cannot be read, is refused (#2634 review F1).
        """
        from .. import config
        with self._connect() as con:
            first = con.execute('SELECT repo,seat,pr,head,turn_key FROM runs WHERE id=?',
                                (run_id,)).fetchone()
        st, lock, loop = retry_terms.locks(first)
        pushes = first is not None and first['seat'] in ('fixer', 'issue_fixer')
        with lock, (config.push_policy_lock() if pushes else nullcontext()), \
                self._connect() as con:
            admitted, policy_off = retry_terms.policy(first, st)
            con.execute('BEGIN IMMEDIATE')
            refused = retry_terms.refusal(con, run_id, policy_off, budget)
            # Only once every check passed (#2634 review F2): a refused retry keeps no snapshot.
            shadow = None if refused else retry_terms.owed_shadow(self, con, loop, first)
            if refused or isinstance(shadow, str):
                con.execute('COMMIT')
                raise ValueError(refused or shadow)
            if admitted is not None:
                con.execute('UPDATE runs SET push_admitted=? WHERE id=?', (admitted, run_id))
            self._rearm(con, run_id, reset=True, budget=budget)
            if shadow is not None:
                self._insert_runs(con, [], shadow, (first['repo'], first['pr'], first['head'],
                                                    first['turn_key']), time.time())
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
                # An operator's stop is never one an event re-arms, even one asked first for
                # the shadow the turn owes (#2634 review F5): its cause becomes the operator's.
                con.execute("UPDATE runs SET cancel_requested=COALESCE(cancel_requested, ?), "
                            "error=CASE WHEN substr(error, 1, ?)=? THEN ? ELSE error END, "
                            "updated=? WHERE id=? AND state=?",
                            (now, len(SHADOW_OWED), SHADOW_OWED, run_supervisor.OPERATOR_CANCEL,
                             now, run_id, was))
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
