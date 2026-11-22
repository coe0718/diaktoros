"""The ledger's records of the issues a shadow review or an adjudicator asks to file (#914, #1864).

``shadow_issues`` holds each request (the seat's broker is no-write, so it is recorded, never
posted by the seat); ``filed_issues`` holds the host's filing of one, committed before its POST,
beside the reviewer's own. Split out of ``records`` (#2585): ``records.LedgerRecords`` inherits
``ShadowIssueRecords``, so ``Supervisor`` has these methods, and a test's
``patch.object(Supervisor, ...)`` patches them, as before.
"""
from __future__ import annotations

import json
import time

from .. import run_supervisor


class ShadowIssueRecords:
    """The shadow-issue methods of :class:`~diaktoros.run_supervisor.Supervisor`; ``_connect()``
    is the one ``Supervisor`` provides."""

    def record_shadow_issue(self, run_id: str, repo: str, number: int, head: str, title: str,
                            body: str, labels: list, limit: int, seat: str = 'shadow',
                            shadow_files: bool = True) -> int:
        """Record one issue a live shadow run asked to file (#914), or an adjudicator run (#1864,
        ``seat``); its sequence in the run. Refused (``ValueError``) like the reviewer's
        (``record_filed_issue``): any run but a live one of ``seat`` on this PR and head, past
        ``limit`` in the run, or a title already recorded here. For an adjudicator, an earlier
        request for the title blocks it only while that one is or may be filed: one that never
        will be (denied; a shadow's while ``shadow_files`` is off; an ended adjudicator run's that
        never ruled) gives way to it (review of #1874, F1): it is kept, denied, under a title key
        of its own, so its run's sequence and cap stand (F3). A shadow's still to be filed may
        never be: the adjudicator's is recorded beside it, under a title key of its own, the
        fallback ``ruling_issues`` files only if the shadow's never is (review of #2585, F1)."""
        key = " ".join(title.casefold().split())
        fallback = f"{key}#fallback:{run_id}"
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT repo,pr,head,seat,state,launch_intent FROM runs WHERE id=?',
                              (run_id,)).fetchone()
            if (row is None or (row['repo'], row['pr'], row['head'], row['seat']) !=
                    (repo, number, head, seat) or row['launch_intent'] is None
                    or row['state'] not in ('launching', 'running')):
                raise ValueError(f'{seat} run identity unavailable')
            earlier = con.execute(
                'SELECT i.run_id, i.seq, i.state, r.seat, r.state AS run_state, g.verdict '
                'FROM shadow_issues i JOIN runs r ON r.id=i.run_id LEFT JOIN rulings g ON '
                'g.run_id=i.run_id WHERE i.repo=? AND i.pr=? AND i.title_key=?',
                (repo, number, key)).fetchone()
            if seat == 'adjudicator' and con.execute(
                    'SELECT 1 FROM shadow_issues WHERE run_id=? AND title_key=?',
                    (run_id, fallback)).fetchone():
                raise ValueError('an issue with this title was already asked for from this PR')
            if earlier is not None:
                never = (earlier['state'] == 'denied' or earlier['state'] == 'recorded' and (
                    (earlier['seat'] == 'shadow' and not shadow_files)
                    or (earlier['seat'] == 'adjudicator' and earlier['verdict'] is None
                        and earlier['run_state'] not in run_supervisor.NOT_ENDED)))
                due = earlier['seat'] == 'shadow' and earlier['state'] in ('recorded', 'posting')
                if seat == 'adjudicator' and due and not never:
                    key = fallback                     # beside the shadow's, never in its place
                elif seat != 'adjudicator' or not never:
                    raise ValueError('an issue with this title was already asked for from this PR')
                else:
                    # Kept, not deleted (review of #1874, F3): its run's sequence and per-run cap
                    # stand, so a live shadow's next request takes a fresh sequence. Only its
                    # title key steps aside, and it can never be filed.
                    con.execute("UPDATE shadow_issues SET state='denied', title_key=?, error=?, "
                                "updated=? WHERE run_id=? AND seq=?",
                                (f"{key}#gave-way:{run_id}",
                                 f"gave way to adjudicator run {run_id}", time.time(),
                                 earlier['run_id'], earlier['seq']))
            count = con.execute('SELECT COUNT(*) FROM shadow_issues WHERE run_id=?',
                                (run_id,)).fetchone()[0]
            if count >= limit:
                raise ValueError(f'at most {limit} issues per review')
            now = time.time()
            con.execute('INSERT INTO shadow_issues(run_id,seq,repo,pr,head,title,title_key,body,'
                        'labels,state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (run_id, count + 1, repo, number, head, title, key, body,
                         json.dumps(list(labels)), 'recorded', now, now))
            con.execute('COMMIT')
            return count + 1

    def shadow_issues_waiting(self, repo: str, seat: str = 'shadow') -> list:
        """The issues runs of ``seat`` asked for, recorded and not yet filed or ruled out (#914,
        #1864), oldest first; an adjudicator's with its run's ruling (``verdict``, NULL for none
        yet), run state (``run_state``) and the shadow review it ruled on (``shadow_run``)."""
        with self._connect() as con:
            return con.execute(
                "SELECT i.*, g.verdict AS verdict, r.state AS run_state, g.shadow_run AS "
                "shadow_run FROM shadow_issues i "
                "JOIN runs r ON r.id=i.run_id LEFT JOIN rulings g ON g.run_id=i.run_id "
                "WHERE i.repo=? AND i.state='recorded' AND r.seat=? "
                "ORDER BY i.created, i.run_id, i.seq", (repo, seat)).fetchall()

    def shadow_requests(self, run_id: str, repo: str, number: int, head: str) -> list:
        """``(title, body, state)`` of each issue one shadow run asked to file at this PR and head,
        but any that never will be (``denied``): what a ruling over it need not ask again (#2209)."""
        with self._connect() as con:
            return con.execute("SELECT title, body, state FROM shadow_issues WHERE run_id=? AND "
                               "repo=? AND pr=? AND head=? AND state!='denied' ORDER BY seq",
                               (run_id, repo, number, head)).fetchall()

    def record_shadow_filing(self, run_id: str, repo: str, number: int, head: str, title: str,
                             depth: int, claim: tuple[int, int] | None = None) -> int | None:
        """The ``filed_issues`` row of an issue the host files for a shadow review (#914), before
        the POST, so the origin-PR hold (#324) and the lineage bound (#487) see it as they see the
        reviewer's. Its sequence in that run; ``ValueError`` when this PR already filed the title.
        ``claim`` (``(seq, attempts)`` of the ``shadow_issues`` row the caller claimed) fences it
        (review of #2585, F7): None, nothing written, unless that row is still ``posting`` under
        that claim, so a sweep resumed after ``settle_stale`` ended its claim never POSTs."""
        key = " ".join(title.casefold().split())
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if claim is not None and not con.execute(
                    "SELECT 1 FROM shadow_issues WHERE run_id=? AND seq=? AND attempts=? AND "
                    "state='posting'", (run_id, *claim)).fetchone():
                con.execute('ROLLBACK')
                return None
            if con.execute('SELECT 1 FROM filed_issues WHERE repo=? AND pr=? AND title_key=?',
                           (repo, number, key)).fetchone():
                con.execute('ROLLBACK')
                raise ValueError('an issue with this title was already filed from this PR')
            # MAX, not COUNT: release_shadow_filing leaves holes, and a count would reuse a
            # sequence another sweep of this run already holds.
            seq = con.execute('SELECT COALESCE(MAX(seq),0) FROM filed_issues WHERE run_id=?',
                              (run_id,)).fetchone()[0] + 1
            now = time.time()
            con.execute('INSERT INTO filed_issues(run_id,seq,repo,pr,head,title,title_key,depth,'
                        'state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (run_id, seq, repo, number, head, title, key, depth, 'recorded', now, now))
            con.execute('COMMIT')
            return seq

    def release_shadow_filing(self, run_id: str, seq: int) -> None:
        """Take back a ``record_shadow_filing`` row whose POST was refused with nothing written, so
        the shadow issue can be filed again; only a row still ``recorded``, never one posted."""
        with self._connect() as con:
            con.execute("DELETE FROM filed_issues WHERE run_id=? AND seq=? AND state='recorded'",
                        (run_id, seq))

    def filed_titles(self, repo: str, number: int, in_flight: bool | None = None) -> list[str]:
        """The normalized titles of every issue filed (or being filed) from a PR, whoever filed
        it: the reviewer, a shadow review or the adjudicator (#1978). ``in_flight`` True: only a
        shadow's or an adjudicator's POST still under way (``recorded``: it may yet be refused and
        released); False: all but those (review of #2585, F1)."""
        flying = ("f.state='recorded' AND f.run_id IN (SELECT id FROM runs WHERE seat IN "
                  "('shadow','adjudicator'))")
        where = {None: '', True: f' AND {flying}', False: f' AND NOT ({flying})'}[in_flight]
        with self._connect() as con:
            return [r[0] for r in con.execute('SELECT f.title_key FROM filed_issues f WHERE '
                                              'f.repo=? AND f.pr=?' + where,
                                              (repo, number)).fetchall()]

    def shadow_model(self, run_id: str) -> str:
        """The model of the shadow review a run recorded, or ''."""
        with self._connect() as con:
            row = con.execute('SELECT model FROM shadow_reviews WHERE run_id=?', (run_id,)).fetchone()
        return str(row[0] or '') if row else ''

    def shadow_issues_posted_since(self, repo: str, since: float, seat: str = 'shadow') -> int:
        with self._connect() as con:
            return con.execute("SELECT COUNT(*) FROM shadow_issues i JOIN runs r ON "
                               "r.id=i.run_id WHERE i.repo=? AND r.seat=? AND i.state IN "
                               "('posting','posted','unlabelled','uncertain') AND i.updated>=?",
                               (repo, seat, since)).fetchone()[0]

    def shadow_issue_status(self, run_id: str, seq: int, state: str, *, expect: str | None = None,
                            issue_number: int | None = None, error: str | None = None,
                            attempts: int | None = None) -> bool:
        """Move one shadow issue to ``state``; with ``expect``, only from that state (a claim), and
        with ``attempts``, only while it has that many (the claim ``record_shadow_filing`` fences)."""
        fence = [(sql, arg) for sql, arg in ((' AND state=?', expect), (' AND attempts=?', attempts))
                 if arg is not None]
        with self._connect() as con:
            cur = con.execute('UPDATE shadow_issues SET state=?,issue_number=COALESCE(?,issue_number),'
                              'error=COALESCE(?,error),updated=? WHERE run_id=? AND seq=?'
                              + ''.join(sql for sql, _ in fence),
                              (state, issue_number, error, time.time(), run_id, seq,
                               *(arg for _, arg in fence)))
        return cur.rowcount == 1

    def shadow_issue_refused(self, run_id: str, seq: int, reason: str, limit: int) -> str:
        """Count one POST refused with nothing written (#1732) against a claimed (``posting``)
        shadow issue: back to ``recorded`` with ``reason``, or ``denied`` once ``limit`` such
        attempts are used. The state it moved to, '' when the row was not ``posting``."""
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute("SELECT attempts FROM shadow_issues WHERE run_id=? AND seq=? "
                              "AND state='posting'", (run_id, seq)).fetchone()
            if row is None:
                con.execute('ROLLBACK')
                return ''
            attempts = row[0] + 1
            state = 'denied' if attempts >= limit else 'recorded'
            note = (f'not sent, dropped after {attempts} refused attempts: {reason}'
                    if state == 'denied' else f'not sent, retried next sweep: {reason}')
            con.execute('UPDATE shadow_issues SET state=?,attempts=?,error=?,updated=? '
                        'WHERE run_id=? AND seq=?',
                        (state, attempts, note[:200], time.time(), run_id, seq))
            con.execute('COMMIT')
            return state
