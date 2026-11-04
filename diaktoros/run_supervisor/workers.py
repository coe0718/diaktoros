"""The worker's lifecycle and recovery (#1349, moved out of the core): starting the isolated
worker (``_spawn``, ``_worker_log``), a running turn's lease heartbeat (``_heartbeat``), and
what settles runs whose worker is gone: ``recover``, ``complete_uncertain``,
``reconcile_uncertain`` (with ``_capacity_for``), ``retire_stale_waiting``, and the CI-hold
linger (``linger_for_ci``).

``WorkerLifecycle`` is a mixin: ``Supervisor`` inherits it, so ``Supervisor(db).recover()`` and a
test's ``patch.object(Supervisor, ...)`` work as before, with one binding. It uses the
``_connect()`` ``Supervisor`` provides, and the package's names through the package
(``run_supervisor.X``), so a test patching ``run_supervisor.X`` still reaches this code.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

from .. import envnames, run_supervisor, util
from ..hostdirs import WORKER_ENV, in_worker
from . import claims


def process_state(pid: int | None, launched: float | None) -> str:
    """Whether the process recorded for a run is ``gone``, ``alive`` or ``unknown``.

    ``gone`` only on proof: no process holds the PID, or its holder provably started after
    the run launched. A present holder is ``alive``; no recorded PID, or one this host may
    not signal, is ``unknown``. An expired lease alone never shows a turn has stopped (#1461).
    """
    if pid is None:
        return 'unknown'
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return 'gone' if exc.errno == errno.ESRCH else 'unknown'
    return 'gone' if run_supervisor._pid_reused(pid, launched) else 'alive'


LINGER_MARK = '.linger.'


def linger_mark(db: Path, pid: int) -> Path:
    """The file ``<ledger>.linger.<pid>`` that process ``pid`` keeps beside the ledger while it
    is inside ``linger_for_ci`` (#1811): the mark by which a test's cleanup tells a lingering
    worker from a stuck process."""
    db = Path(db)
    return db.with_name(f'{db.name}{LINGER_MARK}{pid}')


def _marks(db: Path) -> dict[int, Path]:
    """Every mark file beside the ledger ``db``, live process or not, by the PID in its name."""
    db = Path(db)
    prefix = db.name + LINGER_MARK
    try:
        paths = list(db.parent.iterdir())
    except OSError:
        return {}
    return {int(path.name[len(prefix):]): path for path in paths
            if path.name.startswith(prefix) and path.name[len(prefix):].isdigit()}


def _held(path: Path) -> bool:
    """True while some process holds the lock the lingerer keeps on its mark. The kernel
    drops that lock when the holder leaves the wait, exits or is killed, so unlike the file
    it cannot outlive the wait: a PID reused by a later process, or a mark whose removal
    failed, never reads as held. Anything unreadable is not held."""
    try:
        with open(path, 'rb') as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(probe, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def lingering_pids(db: Path) -> set[int]:
    """The processes that are inside a ``lingering`` block beside the ledger ``db``: those
    whose mark file is locked by its holder at this moment."""
    return {pid for pid, path in _marks(db).items() if _held(path)}


def _unlink(path: Path) -> None:
    """Remove a mark; failing to is not worth stopping a worker for."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _take_mark(mark: Path) -> int | None:
    """Create ``mark`` and lock it; the open descriptor, whose lock is the mark, or None when
    that cannot be done. A sweeper may unlink the file between creating and locking it, so the
    lock only counts if the path still names the file locked."""
    for _ in range(3):
        try:
            fd = os.open(mark, os.O_WRONLY | os.O_CREAT, 0o600)
        except OSError:
            return None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.path.samestat(os.fstat(fd), os.stat(mark)):
                return fd
        except OSError:
            pass
        os.close(fd)
    return None


@contextlib.contextmanager
def lingering(db: Path):
    """Mark this process as lingering for the ``with`` block, which must be only a wait that
    is safe to end by SIGTERM. The mark is ``<ledger>.linger.<pid>`` holding an ``flock`` that
    this process keeps until it leaves the block, however it leaves (#1811): the lock, not the
    file, says it lingers, so a mark left by a killed worker, by a PID since reused or by a
    removal that failed authenticates nobody. Best effort throughout: a worker never creates
    host state, so a ledger directory that is gone or unwritable leaves it unmarked, and a
    mark that cannot be removed is left, unlocked. The next worker to linger sweeps those."""
    for pid, path in _marks(db).items():
        if pid != os.getpid() and not _held(path):
            _unlink(path)
    mark = linger_mark(db, os.getpid())
    fd = _take_mark(mark)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)                    # releases the lock: the mark stops counting here
            _unlink(mark)


class WorkerLifecycle:
    """The worker-lifecycle and recovery methods of :class:`~diaktoros.run_supervisor.Supervisor`."""

    def _spawn(self):
        # Trusted supervisor process only, never the credential-owning gateway agent.
        if not self.fixture_mode and not self.production_config:
            return
        operation = "_fixture-worker" if self.fixture_mode else "_production-worker"
        command = json.dumps(self.fixture_command) if self.fixture_mode else str(self.production_config)
        args = [sys.executable, "-m", "diaktoros.run_supervisor", operation,
                str(self.db), command, json.dumps(self.capacity),
                str(self.lease_seconds), str(self.child_timeout)]
        from ..config.host_guards import (TEST_GUARD_SENTINEL_ENV, TEST_HOME_GUARD_ENV,
                                         TEST_REAL_HOME_ENV, guard_real_home)
        host_home = guard_real_home(self.hermes_home or Path(os.environ.get("HERMES_HOME", os.environ["HOME"])).resolve(strict=True))
        plugin_root = Path(__file__).resolve().parents[2]
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(plugin_root),
               "HOME": str(host_home), "HERMES_HOME": str(host_home),
               envnames.name("TEST_FIXTURE"): "1" if self.fixture_mode else "0"}
        # The worker's environment is built from scratch; keep the test tripwire armed in it.
        for name in (TEST_HOME_GUARD_ENV, TEST_GUARD_SENTINEL_ENV, TEST_REAL_HOME_ENV):
            if os.environ.get(name):
                env[name] = os.environ[name]
        if envnames.get("GH_STUB") and self.fixture_mode:
            env[envnames.name("GH_STUB")] = envnames.get("GH_STUB")
        for name in run_supervisor.HOST_LIMIT_ENV:
            if os.environ.get(name):
                env[name] = os.environ[name]
        if self.fixture_mode:
            util.leak_guard_env(env)

        env[WORKER_ENV] = "1"  # hostdirs: a worker never creates host state (#108)
        from .. import app_tokens, gh_calls
        gh_calls.prepare(host_home)       # so the worker can count its GitHub calls there
        app_tokens.prepare(host_home)     # and share the host's App tokens (#1780)
        run_supervisor._WORKERS[:] = [worker for worker in run_supervisor._WORKERS if worker.poll() is None]
        log = self._worker_log()
        try:
            # ``python -m`` puts the current directory first on sys.path, ahead of PYTHONPATH:
            # started from a checkout of another revision, the worker would run that code
            # (#1373). In the plugin root, the package it imports is this one.
            run_supervisor._WORKERS.append(subprocess.Popen(args, env=env, cwd=str(plugin_root),
                                             stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL,
                                             stderr=log if log is not None else subprocess.DEVNULL,
                                             close_fds=True, start_new_session=True))
        finally:
            if log is not None:
                log.close()  # the worker holds its own copy of the descriptor

    def _worker_log(self):
        """The worker's stderr: ``<ledger>.workers.log``, so its exit reason is on record.

        The host creates it (0600) and rotates it once to ``.1`` past WORKER_LOG_MAX. A worker
        (whose recover() also spawns) only appends to an existing, unrotated log and never
        creates one; failing that its worker's stderr is discarded, as before.
        """
        path = self.db.with_name(self.db.name + ".workers.log")
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            if in_worker():
                if not path.is_file() or path.stat().st_size > run_supervisor.WORKER_LOG_MAX:
                    return None
            else:
                if path.is_file() and path.stat().st_size > run_supervisor.WORKER_LOG_MAX:
                    os.replace(path, path.with_name(path.name + ".1"))
                flags |= os.O_CREAT
            return os.fdopen(os.open(path, flags, 0o600), "ab")
        except OSError:
            return None

    def recover(self) -> str:
        """Sweep lost claims and ambiguous launches; schedule waiting work."""
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            # An operator's cancel (#1148) on a run whose worker is gone is never read by a
            # heartbeat: end it here as the worker would have (#1461). Cancelled before any
            # write-ahead record; uncertain, naming the cancel, after one. A push intent keeps
            # the push quarantine below, which complete_uncertain and reconcile key on.
            # An expired lease is not proof the turn stopped (a heartbeat can fail while the
            # child runs): only a run whose recorded process is provably gone is ended here.
            # A live or unprovable one stays quarantined below (uncertain, still holding its
            # PR and seat, cancel_requested kept) until an operator reconciles it.
            for lost in con.execute("SELECT id,pid,launch_intent FROM runs WHERE state IN "
                                    "('launching','running') AND lease<? AND cancel_requested "
                                    "IS NOT NULL AND push_intent IS NULL", (now,)).fetchall():
                if process_state(lost['pid'], lost['launch_intent']) != 'gone':
                    continue
                wrote = run_supervisor.write_records(con, lost['id'])
                if wrote is None:
                    state, error = 'cancelled', run_supervisor.OPERATOR_CANCEL
                else:
                    state = 'uncertain'
                    error = (f"{run_supervisor.OPERATOR_CANCEL} after it may have written ({wrote}) — "
                             "never replayed; inspect and reconcile")
                con.execute("UPDATE runs SET state=?, error=?, lease=NULL, retry_at=NULL, "
                            "updated=?, finished=? WHERE id=?", (state, error, now, now, lost['id']))
            # Never retry an attempt whose child may have been launched.
            con.execute("UPDATE runs SET state='uncertain', "
                        "error=CASE WHEN push_intent IS NOT NULL THEN "
                        "'post-write push quarantine: worker lost with push intent' "
                        "ELSE 'worker lost after launch intent' END, "
                        "updated=? WHERE state IN ('launching','running') AND lease<?",
                        (now, now))
            con.execute("UPDATE runs SET state='pending', owner=NULL, lease=NULL, updated=? "
                        "WHERE state='claimed' AND lease<? AND attempts<?",
                        (now, now, run_supervisor.MAX_ATTEMPTS))
            con.execute("UPDATE runs SET state='failed', error='claim retry limit', "
                        "owner=NULL, lease=NULL, updated=? WHERE state='claimed' "
                        "AND lease<? AND attempts>=?",
                        (now, now, run_supervisor.MAX_ATTEMPTS))
            # A pre-write failure whose backoff has elapsed is scheduled again (#53).
            con.execute("UPDATE runs SET state='pending', owner=NULL, lease=NULL, attempts=0, "
                        "retry_at=NULL, updated=? WHERE state='waiting' AND "
                        "(retry_at IS NULL OR retry_at<=?)", (now, now))
            pending = con.execute("SELECT COUNT(*) FROM runs WHERE state='pending'").fetchone()[0]
            con.execute("COMMIT")
        if pending and (self.fixture_mode or self.production_config):
            self._spawn()
        return run_supervisor.SILENT

    def _heartbeat(self, run_id: str, owner: str, stop: threading.Event,
                   cancel: threading.Event | None = None) -> None:
        # Only the owning worker can extend a live lease. An expired lease is
        # never silently revived after recovery has quarantined the turn. Each beat also
        # reads an operator's cancel (#1148) into ``cancel``, which stops the turn.
        reported = set()
        while not stop.wait(max(0.01, min(self.lease_seconds / 3, 5))):
            try:
                with self._connect() as con:
                    now = time.time()
                    con.execute("UPDATE runs SET lease=?, updated=? WHERE id=? AND owner=? "
                                "AND state IN ('launching','running') AND lease>=?",
                                (now + self.lease_seconds, now, run_id, owner, now))
                    asked = con.execute("SELECT cancel_requested FROM runs WHERE id=? AND owner=?",
                                        (run_id, owner)).fetchone()
                if cancel is not None and asked is not None and asked['cancel_requested'] is not None:
                    cancel.set()
            except (sqlite3.Error, run_supervisor.LedgerMissing) as exc:
                # Recovery will quarantine this run if persistence stays down; a worker whose
                # ledger is gone never recreates it and ends when its run does. Each distinct
                # failure is logged once (the worker log), so a missed beat has a reason.
                reason = f"{type(exc).__name__}: {exc}"
                if reason not in reported:
                    reported.add(reason)
                    print(f"diaktoros worker {os.getpid()}: heartbeat for run {run_id} "
                          f"failed: {reason}", file=sys.stderr, flush=True)

    def complete_uncertain(self, run_id: str, owner: str, rc: int | None,
                           error: str | None = None, *, stopped: bool = True,
                           retry: bool = False, detail: str | None = None,
                           paced_until: float | None = None,
                           cancel: bool = False) -> str | None:
        """Record direct worker completion, including after lease expiry.

        Never use this for operator guesswork: only the owner after its child
        has stopped may call it. A lost worker remains uncertain until manual
        reconciliation, and is never automatically retried.

        A stopped run with no write-ahead record (``write_records``) failed *before any
        write*: with ``retry`` (a transient cause, or a non-zero sandbox exit) it waits for a
        backed-off relaunch, up to MAX_RETRIES, then fails. A fixer turn the policy held
        before launch (#81) is ``cancelled`` with that reason, like a claim-time policy
        cancellation: no relaunch, listed and recovered by an operator ``retry``. A run
        ``paced_until`` a time (#219: the seat's usage window is closed, or its daily cap is
        reached) waits until then *without* spending a retry — it was never the turn's fault —
        under the same rule: only when nothing was written. A run an operator asked to stop
        (#1148, ``cancel_requested``) that would wait or fail is ``cancelled`` instead, never
        retried — or ``uncertain`` when it holds a write-ahead record. Returns the state written,
        or None when this owner no longer holds the run.
        """
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            ambiguous = con.execute("SELECT 1 FROM review_receipts WHERE run_id=? AND state='claimed'",
                                    (run_id,)).fetchone()
            held = con.execute("SELECT error FROM runs WHERE id=? AND owner=? AND state='uncertain'",
                               (run_id, owner)).fetchone()
            intent = con.execute('SELECT push_intent,retries FROM runs WHERE id=? AND owner=?',
                                 (run_id, owner)).fetchone()
            current = con.execute('SELECT error FROM runs WHERE id=? AND owner=?',
                                  (run_id, owner)).fetchone()
            ineligible = (current is not None
                          and (current['error'] or '').startswith(run_supervisor.REVIEWED_PR_INELIGIBLE))
            quarantined = held is not None and (held['error'] or '').startswith('post-write push quarantine: ')
            post_write = quarantined or (intent is not None and intent['push_intent'] is not None)
            if rc not in (None, 0) and error is None:
                error = f'turn exited with status {rc}'
            retries = (intent['retries'] or 0) if intent is not None else 0
            retry_at = None
            if ineligible and stopped and not ambiguous and not post_write:
                state, error = 'failed', current['error']   # final: the review exists, never replayed
            elif not stopped or ambiguous or post_write:
                state = 'uncertain'
                error = (held['error'] if quarantined else
                         'post-write push quarantine: unresolved push intent') if post_write else error
            elif cancel and error:
                state = 'cancelled'   # no longer eligible before launch (#723): not a failure
            elif rc == 0 and error is None:
                state = 'succeeded'
            elif run_supervisor.policy_hold(error):
                state = 'cancelled'   # the policy held this turn before it launched (#81)
            elif paced_until is not None and run_supervisor.write_records(con, run_id) is None:
                state, retry_at = 'waiting', max(float(paced_until), time.time())
            elif retry and run_supervisor.write_records(con, run_id) is None:
                retries += 1
                if retries < run_supervisor.MAX_RETRIES:
                    state, retry_at = 'waiting', time.time() + run_supervisor.backoff(retries)
                else:
                    state = 'failed'
                    error = f'retry limit ({retries} attempts): {error}'
            else:
                state = 'failed'
            asked = con.execute('SELECT cancel_requested FROM runs WHERE id=? AND owner=?',
                                (run_id, owner)).fetchone()
            if (asked is not None and asked['cancel_requested'] is not None
                    and state in ('waiting', 'failed') and not ineligible):
                # An operator asked to stop this turn (#1148), whether the worker stopped it or
                # it ended first: never retried. Cancelled before any write; after a write-ahead
                # record it may have written, so it is quarantined, never replayed.
                wrote = run_supervisor.write_records(con, run_id)
                retry_at = None
                if wrote is None:
                    state, error = 'cancelled', run_supervisor.OPERATOR_STOPPED
                else:
                    state = 'uncertain'
                    error = (f"{run_supervisor.OPERATOR_CANCEL} after it may have written ({wrote}) — never "
                             "replayed; inspect and reconcile")
            changed = con.execute(
                "UPDATE runs SET state=?, outcome=?, error=?, detail=?, "
                "retries=?, retry_at=?, lease=NULL, updated=?, finished=? WHERE id=? AND owner=? "
                "AND state IN ('launching','running','uncertain')",
                (state, rc, error, detail, retries, retry_at, time.time(), time.time(), run_id,
                 owner)).rowcount
            con.execute("COMMIT")
        return state if changed else None

    def _capacity_for(self, repo: str, seat: str) -> int:
        """The seat's capacity from this repo's own loop; the worker's table otherwise."""
        if self.production_config:
            try:
                from .. import config
                loop = config.by_repo(repo)
                if loop is not None:
                    return config.seat_concurrency(loop, seat)
            except Exception:
                pass
        return self.capacity[seat]

    def push_worker_gone(self, run_id: str) -> bool:
        """Whether the run's worker is positively gone (#1763, review of #1802 F1 and F2): checked
        before the host reads any proof, so no worker can write an attempt or push after it. A
        missing PID is not gone, since the launch is recorded before its PID is."""
        with self._connect() as con:
            row = con.execute("SELECT pid,launch_intent FROM runs WHERE id=?",
                              (run_id,)).fetchone()
        return row is not None and _worker_gone(row)

    def reconcile_uncertain(self, run_id: str, *, reason: str,
                            acknowledge_no_live_worker: bool = False) -> bool:
        """Operator-only release after inspecting the turn's external writes.

        A present or inaccessible PID conservatively blocks release. A missing
        PID still requires acknowledgement: absence alone cannot prove that
        a GitHub write did not happen before the worker died.
        """
        if not acknowledge_no_live_worker or not reason or len(reason) > 512:
            raise ValueError('explicit reconciliation acknowledgement and reason required')
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute("SELECT pid,state,launch_intent,repo,pr,head,seat FROM runs WHERE id=?",
                              (run_id,)).fetchone()
            if row is None or row['state'] != 'uncertain':
                con.execute('COMMIT')
                return False
            if row['pid'] is not None:
                found = process_state(row['pid'], row['launch_intent'])
                if found == 'unknown':
                    raise ValueError('cannot establish worker is absent')
                if found == 'alive':
                    raise ValueError('worker PID exists; cannot release uncertain run')
            if row['launch_intent'] is None:
                raise ValueError('missing launch intent; cannot establish worker identity')
            con.execute("UPDATE runs SET state='failed', error=?, lease=NULL, "
                        "push_intent=NULL,updated=? "
                        "WHERE id=? AND state='uncertain'",
                        ('operator reconciliation: ' + reason, time.time(), run_id))
            con.execute('COMMIT')
        # The operator's reconciliation is what ends it, so it frees its claim too.
        _release_claim(row, run_id)
        return True

    def settle_push_quarantine(self, run_id: str, *, evidence: str) -> bool:
        """End a fixer push quarantine the host proved wrote nothing (#1763): ``failed``, its
        intent withdrawn, its seat freed, and re-armable, unlike an operator reconciliation.

        The caller (``push_settle``) established the worker gone first (``push_worker_gone``),
        then proved the GitHub side: the branch and the PR still at the run's head, and no push
        attempt in the broker audit. This checks the ledger side again under its lock: still a
        fixer's push quarantine, never one the host saw publish, never confirmed, and its worker
        still gone. Whether it settled."""
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute("SELECT pid,state,error,launch_intent,push_confirmed,repo,pr,head,"
                              "seat FROM runs WHERE id=?", (run_id,)).fetchone()
            error = str(row['error'] or '') if row is not None else ''
            if (row is None or row['state'] != 'uncertain' or row['seat'] != 'fixer'
                    or not error.startswith(run_supervisor.PUSH_QUARANTINE)
                    or error.startswith(run_supervisor.PUSH_QUARANTINE + 'published')
                    or row['push_confirmed'] is not None or not _worker_gone(row)):
                con.execute('COMMIT')
                return False
            con.execute("UPDATE runs SET state='failed', error=?, lease=NULL, push_intent=NULL, "
                        "updated=? WHERE id=? AND state='uncertain'",
                        (f'{run_supervisor.PUSH_SETTLED} {evidence}'[:600], time.time(), run_id))
            con.execute('COMMIT')
        _release_claim(row, run_id)
        return True

    def retire_stale_waiting(self, repo: str, open_heads: dict) -> int:
        """Cancel the repo's waiting reviewer, fixer and adjudicator runs whose PR is no longer
        open or whose head moved, given a COMPLETE open-PR listing (``{number: head sha}``).

        The claim cancels such a run only when it is drained, which a hold can postpone for days
        (a closed usage window); until then ``stats.now`` and the Desktop bar show it as held.
        Uses the claim's own wording, so ``retry`` refuses it the same way. Returns the count."""
        cancelled = 0
        now = time.time()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("SELECT id,pr,head FROM runs WHERE repo=? AND state='waiting' "
                               "AND seat IN ('reviewer','fixer','adjudicator')", (repo,)).fetchall()
            for row in rows:
                if row['pr'] not in open_heads:
                    why = 'PR closed before the review started'
                elif open_heads[row['pr']] != row['head']:
                    why = 'PR head moved before the review started'
                else:
                    continue
                con.execute("UPDATE runs SET state='cancelled', error=?, retry_at=NULL, updated=? "
                            "WHERE id=? AND state='waiting'", (why, now, row['id']))
                cancelled += 1
            con.execute("COMMIT")
        return cancelled

    def linger_for_ci(self, sleep=time.sleep) -> None:
        """After holding a review for CI, or backing a claim off after a failed GitHub read,
        stay (no seat, no turn) until the next read is due, then schedule it: GitHub sends this
        route no event when CI finishes (#241), and nothing but the watchdog sweep, up to a
        sweep interval later, would otherwise start a row waiting out a backoff (#1302). Only a
        row due within this linger's window is waited for: the second and later read-failure
        backoffs (4 min and up) are the watchdog sweep's, as before."""
        if not self.held_for_ci:
            return
        def marked_sleep(seconds):
            # Marked only while asleep (#1811): a worker in its database reads or in recover()
            # is at work, and a hang there must fail a test's exit check, not be ended.
            with lingering(self.db):
                sleep(seconds)
        self._linger_until_due(marked_sleep)

    def _linger_until_due(self, sleep) -> None:
        """The wait of ``linger_for_ci``: poll the polled holds, sleep to the earliest due row
        and ``recover()`` it, until the window closes or nothing falls due in it."""
        deadline = time.time() + max(run_supervisor.CI_POLL_S, run_supervisor.RETRY_BASE) + 30
        polled = (run_supervisor.CI_HOLD, run_supervisor.VERDICT_HOLD,
                  run_supervisor.CLAIM_READ_FAILED)
        recovered = None
        while time.time() < deadline:
            with self._connect() as con:
                due = con.execute("SELECT MIN(retry_at) FROM runs WHERE state='waiting' AND ("
                                  + " OR ".join("error LIKE ?" for _ in polled) + ")",
                                  tuple(hold + '%' for hold in polled)).fetchone()[0]
            if due is None or due > deadline:
                return          # nothing due in this window: the watchdog sweep is the backstop
            if due <= time.time():
                if due == recovered:
                    return      # recover() did not release it: do not spin on it
                # Not a return: the earliest due row may be another worker's, and the row this
                # worker backed off may fall due later in the window (#1302): keep waiting.
                self.recover()
                recovered = due
                continue
            sleep(min(due - time.time(), deadline - time.time()) + 1)


def _worker_gone(row) -> bool:
    """A recorded launch and PID, and that process gone (``process_state``; unknown is not)."""
    return (row['pid'] is not None and row['launch_intent'] is not None
            and process_state(row['pid'], row['launch_intent']) == 'gone')


def _release_claim(row, run_id: str) -> None:
    """Free an ended uncertain run's seat claim and in-flight mark, which it kept until now
    (#98). Best effort: the ledger row is the decision; a loop no longer configured has none."""
    try:
        from .. import config, gate, state as state_mod
        loop = config.by_repo(row['repo'])
        if loop is not None:
            st = state_mod.state_for(loop)
            # Only the claim this run wrote: the ledger row left `uncertain` already, so a
            # newer run may already hold this seat/PR/head, and keeps its claim and mark.
            if (st.release_if(row['seat'], gate.seat_key(loop, row['pr']), row['head'],
                              run=run_id) and row['seat'] in claims.INFLIGHT_LABEL):
                st.inflight_clear(f"{claims.INFLIGHT_LABEL[row['seat']]}:{row['pr']}:{row['head']}")
    except Exception:
        pass
