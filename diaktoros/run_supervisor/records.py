"""The ledger's record tables (#717): the fixer's answers and disputes, rulings, triage results,
filed issues, issue fixes, regressions, operator actions and shadow reviews (#218), each written before the external
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
                      verdict: str, body: str, shadow_run: str | None = None) -> dict:
        """Durably record the one ruling a live adjudicator run may make.

        ``shadow_run`` is the run id of the shadow review its prompt showed ('' for none), so
        ``gate.shadow_hold`` counts an ACCEPT against that review only (#1112); None (a caller
        that does not know) records NULL, which reads by time as before.

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
                        'updated,shadow_run) VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (run_id, repo, pr, head, row['turn_key'], verdict, body, now, now,
                         shadow_run))
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

    def record_shadow(self, run_id: str, owner: str, entry: dict) -> bool:
        """Record what a shadow turn would have submitted (#218), from the no-write broker's
        entry (``verdict``, ``body``, ``authorized``, ``denial``, ``held``). Owner-scoped, while
        the run is live, once per run. The run's resolved model comes from its own row. Answers
        whether a row was written; a malformed entry or a run not live writes nothing."""
        if (not isinstance(entry, dict) or entry.get("verdict") not in ("APPROVE", "REQUEST_CHANGES")
                or not isinstance(entry.get("body"), str)):
            return False
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT repo,pr,head,seat,model FROM runs WHERE id=? AND owner=? '
                              "AND state IN ('launching','running')", (run_id, owner)).fetchone()
            if row is None or row['seat'] != run_supervisor.SHADOW:
                con.execute('COMMIT')
                return False
            written = con.execute(
                'INSERT OR IGNORE INTO shadow_reviews(run_id,repo,pr,head,model,verdict,body,'
                'authorized,denial,held,created) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (run_id, row['repo'], row['pr'], row['head'], row['model'], entry['verdict'],
                 entry['body'][:60000], 1 if entry.get('authorized') else 0,
                 str(entry.get('denial') or '')[:200] or None,
                 str(entry.get('held') or '')[:400] or None, time.time())).rowcount
            con.execute('COMMIT')
        return written == 1

    def shadow_reviews(self, repo: str | None = None, pr: int | None = None) -> list[dict]:
        """The recorded shadow reviews, oldest first, optionally for one repo or PR."""
        query, args = 'SELECT * FROM shadow_reviews', []
        if repo is not None:
            query, args = query + ' WHERE repo=?', [repo]
            if pr is not None:
                query, args = query + ' AND pr=?', [repo, pr]
        with self._connect() as con:
            return [dict(r) for r in con.execute(query + ' ORDER BY created, run_id', args)]

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

    def record_shadow_issue(self, run_id: str, repo: str, number: int, head: str, title: str,
                            body: str, labels: list, limit: int) -> int:
        """Record one issue a live shadow run asked to file (#914); its sequence in the run.
        Refused (``ValueError``) like the reviewer's (``record_filed_issue``): any run but a live
        shadow on this PR and head, past ``limit`` in the run, or a title already recorded here."""
        key = " ".join(title.casefold().split())
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute('SELECT repo,pr,head,seat,state,launch_intent FROM runs WHERE id=?',
                              (run_id,)).fetchone()
            if (row is None or (row['repo'], row['pr'], row['head'], row['seat']) !=
                    (repo, number, head, 'shadow') or row['launch_intent'] is None
                    or row['state'] not in ('launching', 'running')):
                raise ValueError('shadow run identity unavailable')
            if con.execute('SELECT 1 FROM shadow_issues WHERE repo=? AND pr=? AND title_key=?',
                           (repo, number, key)).fetchone():
                raise ValueError('an issue with this title was already asked for from this PR')
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

    def shadow_issues_waiting(self, repo: str) -> list:
        """The shadow issues recorded and not yet filed or ruled out (#914), oldest first."""
        with self._connect() as con:
            return con.execute("SELECT * FROM shadow_issues WHERE repo=? AND state='recorded' "
                               "ORDER BY created, run_id, seq", (repo,)).fetchall()

    def record_shadow_filing(self, run_id: str, repo: str, number: int, head: str, title: str,
                             depth: int) -> int:
        """The ``filed_issues`` row of an issue the host files for a shadow review (#914), before
        the POST, so the origin-PR hold (#324) and the lineage bound (#487) see it as they see the
        reviewer's. Its sequence in that run; ``ValueError`` when this PR already filed the title."""
        key = " ".join(title.casefold().split())
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if con.execute('SELECT 1 FROM filed_issues WHERE repo=? AND pr=? AND title_key=?',
                           (repo, number, key)).fetchone():
                con.execute('ROLLBACK')
                raise ValueError('an issue with this title was already filed from this PR')
            seq = con.execute('SELECT COUNT(*) FROM filed_issues WHERE run_id=?',
                              (run_id,)).fetchone()[0] + 1
            now = time.time()
            con.execute('INSERT INTO filed_issues(run_id,seq,repo,pr,head,title,title_key,depth,'
                        'state,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (run_id, seq, repo, number, head, title, key, depth, 'recorded', now, now))
            con.execute('COMMIT')
            return seq

    def filed_titles(self, repo: str, number: int) -> list[str]:
        """The normalized titles of the issues the reviewer filed (or is filing) from a PR."""
        with self._connect() as con:
            return [r[0] for r in con.execute('SELECT title_key FROM filed_issues WHERE repo=? '
                                              'AND pr=?', (repo, number)).fetchall()]

    def shadow_model(self, run_id: str) -> str:
        """The model of the shadow review a run recorded, or ''."""
        with self._connect() as con:
            row = con.execute('SELECT model FROM shadow_reviews WHERE run_id=?', (run_id,)).fetchone()
        return str(row[0] or '') if row else ''

    def shadow_issues_posted_since(self, repo: str, since: float) -> int:
        with self._connect() as con:
            return con.execute("SELECT COUNT(*) FROM shadow_issues WHERE repo=? AND state IN "
                               "('posting','posted','uncertain') AND updated>=?",
                               (repo, since)).fetchone()[0]

    def shadow_issue_status(self, run_id: str, seq: int, state: str, *, expect: str | None = None,
                            issue_number: int | None = None, error: str | None = None) -> bool:
        """Move one shadow issue to ``state``; with ``expect``, only from that state (a claim)."""
        with self._connect() as con:
            cur = con.execute('UPDATE shadow_issues SET state=?,issue_number=COALESCE(?,issue_number),'
                              'error=COALESCE(?,error),updated=? WHERE run_id=? AND seq=?'
                              + (' AND state=?' if expect else ''),
                              (state, issue_number, error, time.time(), run_id, seq,
                               *((expect,) if expect else ())))
        return cur.rowcount == 1

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

    def record_repro_proof(self, run_id: str, repo: str, number: int, base: str,
                           proof: str) -> None:
        """Keep the host's reproduction proof for an issue-fix run (#484); once per run."""
        with self._connect() as con:
            con.execute('INSERT OR IGNORE INTO repro_proofs(run_id,repo,number,base,proof,created) '
                        'VALUES(?,?,?,?,?,?)', (run_id, repo, number, base, proof, time.time()))

    def repro_proof_for_pr(self, repo: str, pr: int) -> tuple[dict, str] | None:
        """The proof of the issue fix that opened ``pr`` and the fix's head, else None."""
        with self._connect() as con:
            row = con.execute(
                'SELECT p.proof, f.new_head FROM issue_fixes f JOIN repro_proofs p '
                'ON p.run_id=f.run_id WHERE f.repo=? AND f.pr_number=? ORDER BY f.created DESC',
                (repo, pr)).fetchone()
        if row is None:
            return None
        try:
            proof = json.loads(row[0])
        except ValueError:
            return None
        return (proof, row[1] or '') if isinstance(proof, dict) else None

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

    def record_override(self, repo: str, pr: int, head: str, kind: str, review_id: int,
                        model: str | None, findings: list[dict[str, str]], merged_at: float,
                        seat: str = 'reviewer') -> bool:
        """Record that a human overrode the loop (#220): True for a new record, False when the
        same (PR, head, kind) is already there, so a rescan never doubles it. ``findings`` is the
        open findings as ``{"id": ..., "category": ...}`` dicts, stored as JSON."""
        with self._connect() as con:
            cur = con.execute('INSERT OR IGNORE INTO overrides(repo,pr,head,kind,seat,model,'
                              'review_id,findings,merged_at,created) VALUES(?,?,?,?,?,?,?,?,?,?)',
                              (repo, int(pr), head, kind, seat, model, int(review_id),
                               json.dumps(list(findings)), merged_at, time.time()))
        return cur.rowcount == 1

    # "agent hold" is a hold by an ``operator_agents`` login (#757): counted apart from a person's.
    OPERATOR_ACTIONS = ("retry", "escalate", "review", "hold", "agent hold", "cancel",
                        "reconsider")

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

    def ruled_approval(self, repo: str, pr: int, head: str) -> dict | None:
        """How the adjudicator acted on its ACCEPT at this head (#905), or None if it has not."""
        with self._connect() as con:
            row = con.execute('SELECT * FROM ruled_approvals WHERE repo=? AND pr=? AND head=?',
                              (repo, pr, head)).fetchone()
        return dict(row) if row else None

    def ruled_approval_step(self, repo: str, pr: int, head: str, step: str, state: str, *,
                            review_id: int | None = None, overruled: int | None = None,
                            error: str | None = None, ruling: str | None = None) -> bool:
        """Move one step (``approve`` or ``dismiss``) of acting on an ACCEPT at this head (#905),
        forward only: a write that may have been sent is never replayed, and ``uncertain`` moves
        only once GitHub has been read. With ``ruling`` (its run ID), the move happens only while
        that ruling is the PR's newest and an ACCEPT, checked in the same transaction: a ruling
        recorded before the claim always stops it. False when the move is refused, as when
        another sweep made it first or a newer ruling landed."""
        allowed = {'pending': {'posting', 'denied', 'none'}, 'posting': {'posted', 'uncertain'},
                   'uncertain': {'posted', 'denied'}}
        if step not in ('approve', 'dismiss'):
            raise ValueError('invalid ruled approval step')
        now = time.time()
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            newest = con.execute('SELECT run_id, verdict FROM rulings WHERE repo=? AND pr=? '
                                 'ORDER BY created DESC LIMIT 1', (repo, pr)).fetchone()
            if ruling is not None and (newest is None or tuple(newest) != (ruling, 'ACCEPT')):
                con.execute('ROLLBACK')
                return False
            con.execute('INSERT OR IGNORE INTO ruled_approvals(repo,pr,head,created,updated) '
                        'VALUES(?,?,?,?,?)', (repo, pr, head, now, now))
            row = con.execute(f'SELECT {step} FROM ruled_approvals WHERE repo=? AND pr=? AND '
                              'head=?', (repo, pr, head)).fetchone()
            if state not in allowed.get(row[0], set()):
                con.execute('ROLLBACK')
                return False
            con.execute(f'UPDATE ruled_approvals SET {step}=?, review_id=COALESCE(?,review_id), '
                        'overruled=COALESCE(?,overruled), error=COALESCE(?,error), updated=? '
                        'WHERE repo=? AND pr=? AND head=?',
                        (state, review_id, overruled, error, now, repo, pr, head))
            con.execute('COMMIT')
        return True

    def rulings(self, limit: int = 50, run_id: str | None = None) -> list[dict]:
        """Read-only operator view of the latest rulings, including their full reason; with
        ``run_id``, only that ruling, however old (#905 cites one by its run)."""
        with self._connect() as con:
            if run_id:
                return [dict(row) for row in con.execute(
                    'SELECT * FROM rulings WHERE run_id=?', (run_id,))]
            return [dict(row) for row in con.execute(
                'SELECT * FROM rulings ORDER BY created DESC, run_id LIMIT ?',
                (max(1, min(int(limit), 200)),))]
