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

import errno
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
                try:
                    os.kill(row['pid'], 0)
                except OSError as exc:
                    if exc.errno != errno.ESRCH:
                        raise ValueError('cannot establish worker is absent') from exc
                else:
                    if not run_supervisor._pid_reused(row['pid'], row['launch_intent']):
                        raise ValueError('worker PID exists; cannot release uncertain run')
            if row['launch_intent'] is None:
                raise ValueError('missing launch intent; cannot establish worker identity')
            con.execute("UPDATE runs SET state='failed', error=?, lease=NULL, "
                        "push_intent=NULL,updated=? "
                        "WHERE id=? AND state='uncertain'",
                        ('operator reconciliation: ' + reason, time.time(), run_id))
            con.execute('COMMIT')
        # The uncertain run kept its seat claim and in-flight mark until now (#98): the
        # operator's reconciliation is what ends it, so it frees them too. Best effort — the
        # ledger row above is the decision; a loop that is no longer configured has no claim.
        try:
            from .. import config, gate, state as state_mod
            loop = config.by_repo(row['repo'])
            if loop is not None:
                st = state_mod.state_for(loop)
                # Only the claim this run wrote: the ledger row left `uncertain` above, so a
                # newer run may already hold this seat/PR/head, and keeps its claim and mark.
                if (st.release_if(row['seat'], gate.seat_key(loop, row['pr']), row['head'],
                                  run=run_id) and row['seat'] in run_supervisor.INFLIGHT_LABEL):
                    st.inflight_clear(f"{run_supervisor.INFLIGHT_LABEL[row['seat']]}:{row['pr']}:{row['head']}")
        except Exception:
            pass
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
        """After holding a review for CI, stay (no seat, no turn) until its next CI read is due,
        then schedule it: GitHub sends this route no event when CI finishes (#241)."""
        if not self.held_for_ci:
            return
        deadline = time.time() + run_supervisor.CI_POLL_S + 30
        while time.time() < deadline:
            with self._connect() as con:
                due = con.execute("SELECT MIN(retry_at) FROM runs WHERE state='waiting' AND "
                                  "error LIKE ?", (run_supervisor.CI_HOLD + '%',)).fetchone()[0]
            if due is None:
                return
            if due <= time.time():
                self.recover()
                return
            sleep(min(due - time.time(), deadline - time.time()) + 1)
