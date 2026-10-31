"""The operator notices (#1349, moved out of the core): the alert for a run that ends failed or
uncertain (``notify``, claim-before-send, one per run) with its wording (``_notice_message``,
``_dispute_message``, ``_ruling_message``), and the observer's ``failed`` notice
(``failure_notice``).

``OperatorNotices`` is a mixin: ``Supervisor`` inherits it, so ``Supervisor(db).notify(...)`` and
a test's ``patch.object(Supervisor, ...)`` work as before, with one binding. It uses the
``_connect()`` ``Supervisor`` provides, and the package's names through the package
(``run_supervisor.X``), so a test patching ``run_supervisor.X`` still reaches this code.
"""
from __future__ import annotations

import time

from .. import run_supervisor
from . import views


class OperatorNotices:
    """The operator-notice methods of :class:`~diaktoros.run_supervisor.Supervisor`."""

    def _dispute_message(self, row) -> str:
        comment = row['comment']
        # Still 'posting' when the notice goes out (its run ended): the outcome is unknown.
        if comment == 'posting':
            comment = 'uncertain'
        body = row['body'].strip()
        if len(body) > 1500:
            body = body[:1500] + (" […truncated; the full answers are the fixer's PR comment]"
                                  if comment == 'posted' else " […truncated]")
        status = {'posted': f"posted on the PR (comment {row['comment_id']})",
                  'denied': f"not posted ({row['comment_error'] or 'authorization denied'})",
                  }.get(comment, 'POST outcome unknown — inspect the PR before any repost')
        return (f"🛑 Diaktoros fixer disputed the review: "
                f"https://github.com/{row['repo']}/pull/{row['pr']} head={row['head']} "
                f"run={row['run_id']}. Fixer's PR comment: {status}. The fixer found no defect "
                "and pushed nothing; the reviewer "
                f"was not re-requested. Decide: {views.dispute_choices(self._loop_id(row), row['pr'])}"
                "; or fix it by hand. Fixer's evidence (model output, not verified by the loop):\n"
                + body)

    def _ruling_message(self, row) -> str:
        body = row['body'].strip()
        if len(body) > 1500:
            body = body[:1500] + ' […truncated; read the full ruling with `python -m ' \
                'diaktoros.run_supervisor rulings DB`]'
        comment = {'pending': 'not attempted (delivery interrupted)',
                   'none': 'not posted (no adjudicator GitHub identity configured)',
                   'denied': f"not posted ({row['comment_error'] or 'authorization denied'})",
                   'posting': 'POST outcome unknown — inspect the PR before any repost',
                   'posted': f"posted on the PR (comment {row['comment_id']})",
                   'uncertain': 'POST outcome unknown — inspect the PR before any repost'
                   }.get(row['comment'], row['comment'])
        return (f"⚖️ Diaktoros adjudicator ruling {row['verdict']}: "
                f"https://github.com/{row['repo']}/pull/{row['pr']} head={row['head']} "
                f"run={row['run_id']} ({row['turn_key']}). PR comment: {comment}. "
                "The adjudicator never merges, pushes or reviews; the decision is yours. "
                f"Reason as written by the adjudicator (model output, not verified by the loop):\n"
                f"{body}")

    @staticmethod
    def _loop_id(row) -> str:
        """The loop a run's repository belongs to, for the commands a notice names; ``LOOP``
        when the config cannot be read (a notice must go out even then)."""
        try:
            from .. import config
            return (config.by_repo(row['repo']) or {}).get('id') or 'LOOP'
        except Exception:
            return 'LOOP'

    @staticmethod
    def _notice_message(row, current, wrote: str | None) -> str:
        loop_id = OperatorNotices._loop_id(row)
        kind = "issues" if row['seat'] in ('triage', 'issue_fixer') else "pull"
        head = (f"⚠️ Diaktoros worker {current['state']}: "
                f"https://github.com/{row['repo']}/{kind}/{row['pr']} "
                f"seat={row['seat']} head={row['head']} run={row['id']}. "
                f"Reason: {current['error'] or 'worker outcome unavailable'}.")
        if current['detail']:
            head += f"\nTurn output (tail):\n{run_supervisor.tail(current['detail'], 1200)}\n"
        if str(current['error'] or '').startswith(run_supervisor.REVIEWED_PR_INELIGIBLE):
            return (head + " The review exists on GitHub and the PR changed under it; it is not "
                    "counted as a clean verdict and this run is final (never retried or "
                    "replayed). Inspect the PR and dismiss or keep the review as appropriate.")
        if wrote is None:
            # Nothing reached GitHub: the host's write-ahead records for this run are empty.
            return (head + f" No external write was made ({current['retries'] or 0} failed "
                    "attempts on record). Fix the cause if it is not transient, then re-arm it: "
                    f"`hermes dk retry --loop {loop_id} --pr {row['pr']} --seat {row['seat']}` "
                    f"(or `python -m diaktoros.run_supervisor retry DB {row['id']}`); a "
                    "redelivered webhook for this head also re-arms it.")
        return (head + f" Possible external write ({wrote}). "
                "Do not replay this turn or release its seat based on a lease alone. "
                "Inspect the worker PID and external GitHub writes; use "
                "`python -m diaktoros.run_supervisor status DB` and "
                "`python -m diaktoros.run_supervisor reconcile DB RUN_ID "
                "--reason REASON --acknowledge-no-live-worker` only after "
                "establishing no worker remains. Failed writes require "
                "manual inspection before any new turn."
                + (f" Issue fix: `hermes dk continue --loop {loop_id} --run {row['id']}` "
                   "inspects GitHub and finishes only the missing stage (never pushes); "
                   f"a maintainer may reply `continue {row['id']}`."
                   if row['seat'] == 'issue_fixer' else ""))

    def notify(self, deliver) -> int:
        """One bounded alert per failed/uncertain run, retried if delivery fails.

        The callback must return only after its transport acknowledges delivery.
        A crash between acknowledgement and the SQLite commit can duplicate a
        notice; the stable run ID lets the receiving operator deduplicate it.
        """
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute("INSERT OR IGNORE INTO operator_notices(run_id,state,created) "
                        "SELECT id,'pending',? FROM runs WHERE state IN ('failed','uncertain') "
                        "AND " + run_supervisor.NOT_SHADOW,
                        (time.time(),))
            con.execute('COMMIT')
            rows = con.execute("SELECT r.id,r.repo,r.pr,r.head,r.seat,r.state,r.error "
                               "FROM operator_notices n JOIN runs r ON r.id=n.run_id "
                               "WHERE n.state='pending' ORDER BY n.created,n.run_id LIMIT 20").fetchall()
        count = 0
        for row in rows:
            message = None
            with self._connect() as con:
                con.execute('BEGIN IMMEDIATE')
                pending = con.execute("SELECT 1 FROM operator_notices WHERE run_id=? "
                                      "AND state='pending'", (row['id'],)).fetchone()
                if pending:
                    current = con.execute("SELECT state,error,detail,retries FROM runs WHERE id=?",
                                          (row['id'],)).fetchone()
                    if current is None or current['state'] not in ('failed', 'uncertain'):
                        con.execute("UPDATE operator_notices SET state='resolved' WHERE run_id=?",
                                    (row['id'],))
                        con.execute('COMMIT')
                        continue
                    message = self._notice_message(row, current, run_supervisor.write_evidence(con, row['id']))
                    # Claim durably before calling a potentially slow transport.
                    # A crash while sending is ambiguous: leave it for an operator,
                    # rather than replaying a possibly acknowledged notification.
                    con.execute("UPDATE operator_notices SET state='sending' "
                                "WHERE run_id=? AND state='pending'", (row['id'],))
                con.execute('COMMIT')
            if not pending:
                continue
            try:
                deliver(message)
            except Exception:
                with self._connect() as con:
                    con.execute("UPDATE operator_notices SET state='pending' "
                                "WHERE run_id=? AND state='sending'", (row['id'],))
                raise
            with self._connect() as con:
                con.execute("UPDATE operator_notices SET state='delivered', delivered=? "
                            "WHERE run_id=? AND state='sending'", (time.time(), row['id']))
            count += 1
        # Facts about the ledger itself (it vanished and was recreated): once each.
        with self._connect() as con:
            events = con.execute("SELECT id FROM ledger_events WHERE state='pending' "
                                 "ORDER BY id LIMIT 20").fetchall()
        for event in events:
            with self._connect() as con:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute("SELECT * FROM ledger_events WHERE id=? AND state='pending'",
                                  (event['id'],)).fetchone()
                if row is not None:
                    con.execute("UPDATE ledger_events SET state='sending' WHERE id=?", (row['id'],))
                con.execute('COMMIT')
            if row is None:
                continue
            try:
                deliver(row['message'])
            except Exception:
                with self._connect() as con:
                    con.execute("UPDATE ledger_events SET state='pending' WHERE id=? "
                                "AND state='sending'", (row['id'],))
                raise
            with self._connect() as con:
                con.execute("UPDATE ledger_events SET state='delivered',delivered=? WHERE id=? "
                            "AND state='sending'", (time.time(), row['id']))
            count += 1
        # Every ruling reaches the operator here, whatever the observer feed's configuration,
        # mute or event filter: the feed is best effort, this outbox is the guaranteed path.
        # Same claim-before-send rule as above: a crash mid-send is left 'sending', never replayed.
        with self._connect() as con:
            rulings = con.execute("SELECT run_id FROM rulings WHERE notice='pending' "
                                  "ORDER BY created,run_id LIMIT 20").fetchall()
        for ruling in rulings:
            with self._connect() as con:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute("SELECT * FROM rulings WHERE run_id=? AND notice='pending'",
                                  (ruling['run_id'],)).fetchone()
                if row is not None:
                    con.execute("UPDATE rulings SET notice='sending',updated=? WHERE run_id=?",
                                (time.time(), row['run_id']))
                con.execute('COMMIT')
            if row is None:
                continue
            try:
                deliver(self._ruling_message(row))
            except Exception:
                with self._connect() as con:
                    con.execute("UPDATE rulings SET notice='pending' WHERE run_id=? "
                                "AND notice='sending'", (row['run_id'],))
                raise
            with self._connect() as con:
                con.execute("UPDATE rulings SET notice='delivered',notice_delivered=?,updated=? "
                            "WHERE run_id=? AND notice='sending'",
                            (time.time(), time.time(), row['run_id']))
            count += 1
        # A fixer's dispute reaches the operator exactly once (#401): claim, send, never replay.
        with self._connect() as con:
            # Not while the comment POST is unanswered and its run is still live: the notice
            # must not lead the comment. A run that ended 'posting' is sent, as uncertain.
            disputes = con.execute(
                "SELECT d.run_id FROM disputes d LEFT JOIN runs r ON r.id=d.run_id "
                "WHERE d.notice='pending' AND NOT (d.comment='posting' AND r.state IN "
                f"({','.join('?' * len(run_supervisor.ACTIVE))})) ORDER BY d.created,d.run_id LIMIT 20",
                run_supervisor.ACTIVE).fetchall()
        for dispute in disputes:
            with self._connect() as con:
                con.execute('BEGIN IMMEDIATE')
                row = con.execute("SELECT * FROM disputes WHERE run_id=? AND notice='pending'",
                                  (dispute['run_id'],)).fetchone()
                if row is not None:
                    con.execute("UPDATE disputes SET notice='sending' WHERE run_id=?",
                                (row['run_id'],))
                con.execute('COMMIT')
            if row is None:
                continue
            try:
                deliver(self._dispute_message(row))
            except Exception:
                with self._connect() as con:
                    con.execute("UPDATE disputes SET notice='pending' WHERE run_id=? "
                                "AND notice='sending'", (row['run_id'],))
                raise
            with self._connect() as con:
                con.execute("UPDATE disputes SET notice='delivered',notice_delivered=? "
                            "WHERE run_id=? AND notice='sending'", (time.time(), row['run_id']))
            count += 1
        return count

    def failure_notice(self, run_id: str, state: str | None) -> None:
        """The observer's ``failed`` notice (#231): a run's first failed attempt, and its end.

        The watchdog's operator outbox reports a run that ends failed or uncertain; this feed
        also says so the moment the first attempt fails, while the run still waits to retry,
        instead of after every retry is spent. One notice per run per kind (its first failure,
        its terminal state). A hold — the seat's daily cap reached, or its provider's usage
        window closed — is not a failure, but a silent one looked exactly like a turn still
        running (live, 2026-10-04): it gets its own ``held`` notice, once per hold, with when it
        resumes and how to run it sooner. Facts only: the host-written error, never the turn's
        own output. Best effort.
        """
        if state not in ("waiting", "failed", "uncertain"):
            return
        try:
            with self._connect() as con:
                row = con.execute("SELECT repo,pr,head,seat,error,retries,retry_at FROM runs "
                                  "WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return
            event = "failed"
            if state == "waiting" and str(row["error"] or "").startswith(run_supervisor.CI_HOLD):
                if " — conflicts with " in str(row["error"]):
                    # A merge conflict waits on a person too: GitHub runs no CI until it is
                    # resolved and the loop cannot do it here. Once per run, not per poll.
                    event, kind = "held", "held:ci-conflict"
                    outcome = (f"{row['seat']} {row['error']} — the review starts once the "
                               "conflict is resolved and CI passes; the loop cannot resolve it here")
                elif "cancelled" not in str(row["error"]):
                    return               # the normal wait for CI (#241): status and explain show it
                else:
                    # Cancelled checks wait on a person (#363): say so once per run, not per poll.
                    event, kind = "held", "held:ci-cancelled"
                    outcome = (f"{row['seat']} {row['error']} — the review starts once they pass; "
                               "the loop cannot re-run CI itself")
            elif state == "waiting" and str(row["error"] or "").startswith("held:"):
                # A pacing hold (#219, #247): the run waits without spending a retry.
                event, kind = "held", f"held:{int(row['retry_at'] or 0)}"
                outcome = f"{row['seat']} {row['error']}"
                flag = run_supervisor.hold_remedy(row["seat"], str(row["error"] or ""))
                if flag:
                    outcome += (f" · to run it sooner, raise the cap (`{flag}`), then `retry "
                                f"--pr {row['pr']} --seat {row['seat']}`")
            elif state == "waiting":
                if (row["retries"] or 0) != 1:
                    return               # a later retry: the first one was already reported
                when = (time.strftime("%H:%M", time.localtime(row["retry_at"]))
                        if row["retry_at"] else "soon")
                outcome = (f"{row['seat']} attempt 1 failed: {row['error']} — retrying at "
                           f"{when}, up to {run_supervisor.MAX_RETRIES} attempts")
                kind = "first"
            else:
                outcome = f"{row['seat']} {state}: {row['error']}"
                kind = state
            from .. import config, observer, state as state_mod
            loop = config.by_repo(row["repo"])
            if loop is None:
                return
            observer.notify(loop, state_mod.state_for(loop), event, row["pr"], row["head"],
                            identity=f"{run_id}:{kind}", outcome=outcome[:400],
                            issue=row["seat"] in ("triage", "issue_fixer"))
        except Exception:
            pass                         # a notice never changes the run
