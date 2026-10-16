"""The ledger's record tables (#717): the fixer's answers and disputes, rulings, triage results,
filed issues, issue fixes, regressions and operator actions, each written before the external
write it records and read back by the operator commands.

``LedgerRecords`` is a mixin: ``Supervisor`` inherits it, so ``Supervisor(db).record_ruling(...)``
and a test's ``patch.object(Supervisor, ...)`` work as before, with one binding. It uses the
``_connect()`` ``Supervisor`` provides, and the package's names through the package.
"""
from __future__ import annotations

import json
import sqlite3
import time

from .. import run_supervisor, util


class LedgerRecords:
    """The record-table methods of :class:`~diaktoros.run_supervisor.Supervisor`."""

    def begin_answers(self, run_id: str, repo: str, pr: int, base: str, head: str,
                      body: str, state: str = 'posting', error: str | None = None,
                      dispute: bool = False) -> None:
        """Durably record the fixer's one answers comment before (or instead of) its POST.

        Only a running fixer run whose push was confirmed may record one, once: the run ID is the
        primary key, so a second attempt is a refusal, never a second comment.
        """
        if state not in ('posting', 'denied') or not isinstance(body, str) or not body.strip():
            raise ValueError('invalid answers record')
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT repo,pr,head,seat,state,launch_intent,push_confirmed,'
                              'partial_view FROM runs WHERE id=?', (run_id,)).fetchone()
            # Answers follow a confirmed push — or, when the host recorded that this fixer could
            # not see the whole change (#93, #110), replace it, at the head it was given.
            # A dispute (#401) likewise answers at the head it was given, with nothing pushed.
            answers_only = bool(row is not None and (row['partial_view'] or dispute)
                                and head == base)
            if (row is None or (row['repo'], row['pr'], row['head'], row['seat']) !=
                    (repo, pr, base, 'fixer') or row['launch_intent'] is None
                    or row['state'] not in ('launching', 'running')
                    or (row['push_confirmed'] is None and not answers_only)):
                raise ValueError('answers run identity unavailable')
            if con.execute('SELECT 1 FROM fixer_answers WHERE run_id=?', (run_id,)).fetchone():
                raise ValueError('answers already recorded')
            now = time.time()
            con.execute('INSERT INTO fixer_answers(run_id,repo,pr,base,head,body,state,error,'
                        'created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (run_id, repo, pr, base, head, body, state, error, now, now))
            con.execute('COMMIT')

    def record_dispute(self, run_id: str, repo: str, pr: int, head: str, body: str) -> None:
        """Record the fixer's dispute at the head it was given: once per run, never a review request."""
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT repo,pr,head,seat,state FROM runs WHERE id=?',
                              (run_id,)).fetchone()
            if (row is None or (row['repo'], row['pr'], row['head'], row['seat']) !=
                    (repo, pr, head, 'fixer') or row['state'] not in ('launching', 'running')):
                con.execute('ROLLBACK')
                raise ValueError('dispute run identity unavailable')
            con.execute('INSERT OR IGNORE INTO disputes(run_id,repo,pr,head,body,created) '
                        'VALUES(?,?,?,?,?,?)', (run_id, repo, pr, head, body, time.time()))
            con.execute('COMMIT')

    def dispute_comment(self, run_id: str, comment: str, *, comment_id: int | None = None,
                        error: str | None = None) -> None:
        """'posting' may only become 'posted', 'uncertain' or 'denied'."""
        if comment not in ('posted', 'uncertain', 'denied'):
            raise ValueError('invalid dispute comment state')
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT comment FROM disputes WHERE run_id=?',
                              (run_id,)).fetchone()
            if row is None or row['comment'] != 'posting':
                con.execute('ROLLBACK')
                raise ValueError('dispute comment transition refused')
            con.execute('UPDATE disputes SET comment=?,comment_id=?,comment_error=? '
                        'WHERE run_id=?', (comment, comment_id, error, run_id))
            con.execute('COMMIT')

    def answers_status(self, run_id: str, state: str, *, comment_id: int | None = None,
                       error: str | None = None) -> None:
        """'posting' may only become 'posted' or 'uncertain' — never pending or posting again."""
        if state not in ('posted', 'uncertain'):
            raise ValueError('invalid answers state')
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT state FROM fixer_answers WHERE run_id=?',
                              (run_id,)).fetchone()
            if row is None or row['state'] != 'posting':
                raise ValueError('answers state transition refused')
            con.execute('UPDATE fixer_answers SET state=?,comment_id=?,error=?,updated=? '
                        'WHERE run_id=?', (state, comment_id, error, time.time(), run_id))
            con.execute('COMMIT')

    def answers(self, limit: int = 50) -> list[dict]:
        """Read-only operator view of the fixer answers comments and how each POST ended."""
        with self._connect() as con:
            return [dict(row) for row in con.execute(
                'SELECT * FROM fixer_answers ORDER BY created DESC, run_id LIMIT ?',
                (max(1, min(int(limit), 200)),))]

    def record_ruling(self, run_id: str, repo: str, pr: int, head: str,
                      verdict: str, body: str) -> dict:
        """Durably record the one ruling a live adjudicator run may make.

        This commit is the ruling's acknowledgement: it happens before any notice or GitHub
        write, so a failed transport can never lose it, and the run ID primary key makes a
        replayed request a refusal rather than a second ruling.
        """
        if verdict not in run_supervisor.RULINGS or not isinstance(body, str) or not body.strip():
            raise ValueError('invalid ruling')
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT repo,pr,head,seat,state,launch_intent,turn_key '
                              'FROM runs WHERE id=?', (run_id,)).fetchone()
            if (row is None or (row['repo'], row['pr'], row['head'], row['seat']) !=
                    (repo, pr, head, 'adjudicator') or row['launch_intent'] is None
                    or row['state'] not in ('launching', 'running')):
                raise ValueError('ruling run identity unavailable')
            if con.execute('SELECT 1 FROM rulings WHERE run_id=?', (run_id,)).fetchone():
                raise ValueError('ruling already recorded')
            now = time.time()
            con.execute('INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,'
                        'updated) VALUES(?,?,?,?,?,?,?,?,?)',
                        (run_id, repo, pr, head, row['turn_key'], verdict, body, now, now))
            con.execute('COMMIT')
        return {'run_id': run_id, 'turn_key': row['turn_key']}

    def ruling_status(self, run_id: str, *, observer: str | None = None,
                      comment: str | None = None, comment_id: int | None = None,
                      comment_error: str | None = None) -> None:
        """Record how each delivery of an already durable ruling ended.

        A comment never leaves 'posting' except to its outcome: once the POST may have been
        sent, the row can only become posted or uncertain, never pending again.
        """
        if comment is not None and comment not in run_supervisor.COMMENT_STATES:
            raise ValueError('invalid comment state')
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT comment FROM rulings WHERE run_id=?', (run_id,)).fetchone()
            if row is None:
                raise ValueError('ruling not recorded')
            if comment is not None:
                allowed = {'pending': {'none', 'denied', 'posting'},
                           'posting': {'posted', 'uncertain'}}.get(row['comment'], set())
                if comment not in allowed:
                    raise ValueError('comment state transition refused')
            con.execute('UPDATE rulings SET observer=COALESCE(?,observer),'
                        'comment=COALESCE(?,comment),comment_id=COALESCE(?,comment_id),'
                        'comment_error=COALESCE(?,comment_error),updated=? WHERE run_id=?',
                        (observer, comment, comment_id, comment_error, time.time(), run_id))
            con.execute('COMMIT')

    def record_triage(self, run_id: str, repo: str, number: int, labels: list[str],
                      body: str) -> None:
        """Durably record the one triage a live triage run may make (#213), before any write.

        Like a ruling, this commit is the acknowledgement: the run ID primary key turns a replayed
        request into a refusal, and a run with this row is never re-armed (``write_records``).
        """
        if (not isinstance(labels, list) or not all(isinstance(x, str) for x in labels)
                or not isinstance(body, str)):
            raise ValueError('invalid triage')
        from ..config import TRIAGE_HEAD
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT repo,pr,head,seat,state,launch_intent FROM runs WHERE id=?',
                              (run_id,)).fetchone()
            if (row is None or (row['repo'], row['pr'], row['head'], row['seat']) !=
                    (repo, number, TRIAGE_HEAD, 'triage') or row['launch_intent'] is None
                    or row['state'] not in ('launching', 'running')):
                raise ValueError('triage run identity unavailable')
            if con.execute('SELECT 1 FROM triage_results WHERE run_id=?', (run_id,)).fetchone():
                raise ValueError('triage already recorded')
            now = time.time()
            con.execute('INSERT INTO triage_results(run_id,repo,number,labels,body,state,created,'
                        'updated) VALUES(?,?,?,?,?,?,?,?)',
                        (run_id, repo, number, json.dumps(labels), body, 'recorded', now, now))
            con.execute('COMMIT')

    def triage_status(self, run_id: str, state: str, *, error: str | None = None,
                      comment_id: int | None = None) -> None:
        with self._connect() as con:
            con.execute('UPDATE triage_results SET state=?,error=COALESCE(?,error),'
                        'comment_id=COALESCE(?,comment_id),updated=? WHERE run_id=?',
                        (state, error, comment_id, time.time(), run_id))

    def record_filed_issue(self, run_id: str, repo: str, number: int, head: str, title: str,
                           depth: int, limit: int) -> int:
        """Durably record one issue a live reviewer run may file (#247), before the POST.

        Returns its sequence number in the run. Refused (``ValueError``) for any run but a live
        reviewer on this PR and head, past ``limit`` issues in the run, or for a title already
        filed on this PR (normalized) — so neither a retry nor a later round files it twice.
        """
        key = " ".join(title.casefold().split())
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT repo,pr,head,seat,state,launch_intent FROM runs WHERE id=?',
                              (run_id,)).fetchone()
            if (row is None or (row['repo'], row['pr'], row['head'], row['seat']) !=
                    (repo, number, head, 'reviewer') or row['launch_intent'] is None
                    or row['state'] not in ('launching', 'running')):
                raise ValueError('reviewer run identity unavailable')
            if con.execute('SELECT 1 FROM filed_issues WHERE repo=? AND pr=? AND title_key=?',
                           (repo, number, key)).fetchone():
                raise ValueError('an issue with this title was already filed from this PR')
            count = con.execute('SELECT COUNT(*) FROM filed_issues WHERE run_id=?',
                                (run_id,)).fetchone()[0]
            if count >= limit:
                raise ValueError(f'at most {limit} issues per review')
            now = time.time()
            con.execute('INSERT INTO filed_issues(run_id,seq,repo,pr,head,title,title_key,depth,'
                        'state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (run_id, count + 1, repo, number, head, title, key, depth, 'recorded',
                         now, now))
            con.execute('COMMIT')
            return count + 1

    def filed_issue_status(self, run_id: str, seq: int, state: str, *,
                           issue_number: int | None = None, error: str | None = None) -> None:
        with self._connect() as con:
            con.execute('UPDATE filed_issues SET state=?,issue_number=COALESCE(?,issue_number),'
                        'error=COALESCE(?,error),updated=? WHERE run_id=? AND seq=?',
                        (state, issue_number, error, time.time(), run_id, seq))

    def filed_depth(self, repo: str, issue_number: int) -> int | None:
        """The lineage depth of an issue a seat filed, or None for one a person opened."""
        with self._connect() as con:
            row = con.execute('SELECT depth FROM filed_issues WHERE repo=? AND issue_number=?',
                              (repo, issue_number)).fetchone()
        return row['depth'] if row is not None else None

    def filed_origin_pr(self, repo: str, issue_number: int) -> int | None:
        """The PR a seat filed this issue from (#324), or None for one a person opened."""
        with self._connect() as con:
            row = con.execute('SELECT pr FROM filed_issues WHERE repo=? AND issue_number=?',
                              (repo, issue_number)).fetchone()
        return row['pr'] if row is not None else None

    def record_issue_fix(self, run_id: str, repo: str, number: int, base: str, kind: str,
                         branch: str) -> None:
        """Durably record the one write a live issue-fix run may make (#214), before it."""
        if kind not in ('pr', 'comment'):
            raise ValueError('invalid issue fix')
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT repo,pr,head,seat,state,launch_intent FROM runs WHERE id=?',
                              (run_id,)).fetchone()
            if (row is None or (row['repo'], row['pr'], row['head'], row['seat']) !=
                    (repo, number, base, 'issue_fixer') or row['launch_intent'] is None
                    or row['state'] not in ('launching', 'running')):
                raise ValueError('issue fix run identity unavailable')
            if con.execute('SELECT 1 FROM issue_fixes WHERE run_id=?', (run_id,)).fetchone():
                raise ValueError('issue fix already recorded')
            now = time.time()
            con.execute('INSERT INTO issue_fixes(run_id,repo,number,base,kind,branch,state,'
                        'created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                        (run_id, repo, number, base, kind, branch, 'recorded', now, now))
            con.execute('COMMIT')

    def issue_fix_status(self, run_id: str, state: str, *, error: str | None = None,
                         new_head: str | None = None, pr_number: int | None = None,
                         comment_id: int | None = None) -> None:
        with self._connect() as con:
            con.execute('UPDATE issue_fixes SET state=?,error=COALESCE(?,error),'
                        'new_head=COALESCE(?,new_head),pr_number=COALESCE(?,pr_number),'
                        'comment_id=COALESCE(?,comment_id),updated=? WHERE run_id=?',
                        (state, error, new_head, pr_number, comment_id, time.time(), run_id))

    def approving_model(self, repo: str, pr: int, head: str, review_id: int) -> str | None:
        """The resolved model of the reviewer run that posted ``review_id`` at ``head``, else of
        the latest reviewer run at that head; None when the ledger has neither."""
        with self._connect() as con:
            row = con.execute(
                "SELECT r.model FROM runs r JOIN review_receipts x ON x.run_id=r.id "
                "WHERE r.repo=? AND r.pr=? AND r.head=? AND r.seat='reviewer' AND x.review_id=?",
                (repo, pr, head, review_id)).fetchone()
            if row is None:
                row = con.execute("SELECT model FROM runs WHERE repo=? AND pr=? AND head=? AND "
                                  "seat='reviewer' ORDER BY created DESC LIMIT 1",
                                  (repo, pr, head)).fetchone()
        return row[0] if row is not None else None

    def link_regression(self, repo: str, issue: int, pr: int, head: str, review_id: int,
                        model: str | None) -> str:
        """Record (or re-link) a verified regression (#661): ``linked``, ``already`` (the same
        link, unchanged) or ``relinked`` (a retracted link restored)."""
        now = time.time()
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            prior = con.execute('SELECT state FROM regressions WHERE repo=? AND issue=? AND pr=?',
                                (repo, issue, pr)).fetchone()
            if prior is None:
                con.execute('INSERT INTO regressions(repo,issue,pr,head,review_id,model,state,'
                            'created,updated) VALUES(?,?,?,?,?,?,?,?,?)',
                            (repo, issue, pr, head, review_id, model, 'linked', now, now))
                outcome = 'linked'
            elif prior[0] == 'linked':
                outcome = 'already'
            else:
                con.execute("UPDATE regressions SET state='linked',head=?,review_id=?,model=?,"
                            "updated=? WHERE repo=? AND issue=? AND pr=?",
                            (head, review_id, model, now, repo, issue, pr))
                outcome = 'relinked'
            con.execute('COMMIT')
        return outcome

    def retract_regression(self, repo: str, issue: int, pr: int) -> bool:
        """Mark a link retracted (kept, so the report can say so); False when there is none."""
        with self._connect() as con:
            cur = con.execute("UPDATE regressions SET state='retracted',updated=? WHERE repo=? "
                              "AND issue=? AND pr=? AND state='linked'",
                              (time.time(), repo, issue, pr))
        return cur.rowcount == 1

    OPERATOR_ACTIONS = ("retry", "escalate", "review", "hold")

    def record_operator_action(self, repo: str, pr: int, action: str) -> None:
        """Note an operator's step on a PR for the report (#678). Called after the step took
        effect; best effort, so a ledger that refuses the write never fails the step."""
        if action not in self.OPERATOR_ACTIONS:
            raise ValueError(f"unknown operator action {action!r}")
        try:
            with self._connect() as con:
                con.execute('INSERT INTO operator_actions(repo,pr,action,created) VALUES(?,?,?,?)',
                            (repo, int(pr), action, time.time()))
        except (sqlite3.Error, OSError, run_supervisor.LedgerMissing) as exc:
            util.log(f"operator action {action} on {repo}#{pr} not recorded: {exc}")

    def issue_fix_prs(self, repo: str, number: int) -> list[int]:
        """The PRs issue-fix runs opened for issue ``number``, newest first (#671)."""
        with self._connect() as con:
            rows = con.execute("SELECT pr_number FROM issue_fixes WHERE repo=? AND number=? "
                               "AND kind='pr' AND pr_number IS NOT NULL ORDER BY created DESC",
                               (repo, number)).fetchall()
        return [row[0] for row in rows]

    def issue_fix_result(self, run_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute('SELECT * FROM issue_fixes WHERE run_id=?', (run_id,)).fetchone()
        return dict(row) if row else None

    def triage_result(self, run_id: str) -> dict | None:
        with self._connect() as con:
            row = con.execute('SELECT * FROM triage_results WHERE run_id=?', (run_id,)).fetchone()
        return dict(row) if row else None

    def rulings(self, limit: int = 50) -> list[dict]:
        """Read-only operator view of the latest rulings, including their full reason."""
        with self._connect() as con:
            return [dict(row) for row in con.execute(
                'SELECT * FROM rulings ORDER BY created DESC, run_id LIMIT ?',
                (max(1, min(int(limit), 200)),))]
