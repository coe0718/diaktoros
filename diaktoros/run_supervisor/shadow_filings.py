"""The ledger's records of the host's filing of a shadow's or an adjudicator's issue request
(#914, #1864): its ``filed_issues`` row, committed before the POST and released when the POST is
refused with nothing written. Split out of ``shadow_records`` (#2585), whose
``ShadowIssueRecords`` inherits ``ShadowFilingRecords``, so ``Supervisor`` has these methods.
"""
from __future__ import annotations

import time

# A claim ``shadow_issues.settle_stale`` ended once its POST may have been sent (#2585).
STALE_WHY = "interrupted after the POST was claimed; outcome unknown"
# A copy dropped only because another may have been filed (review of #2585, F8): taken up again
# if that one's late, definite refusal shows nothing was.
MAYBE = "duplicate: a copy may have been filed (its POST's outcome is unknown)"


class InFlight(ValueError):
    """The title is held only by a shadow's or an adjudicator's POST still under way."""


class MaybeFiled(ValueError):
    """The title is held only by a filing whose POST's outcome is unknown (``uncertain``)."""


def _kinds(con, repo: str, number: int, keep) -> set[str]:
    """How the filings from a PR whose title key ``keep`` accepts stand: ``filed``, ``maybe``
    (``uncertain``) or ``flying`` (a shadow's or an adjudicator's POST still under way, which may
    yet be refused and released: review of #2585, F1)."""
    rows = con.execute("SELECT f.title_key, f.state, r.seat FROM filed_issues f LEFT JOIN runs r "
                       "ON r.id=f.run_id WHERE f.repo=? AND f.pr=?", (repo, number)).fetchall()
    return {"flying" if state == "recorded" and seat in ("shadow", "adjudicator")
            else "maybe" if state == "uncertain" else "filed"
            for key, state, seat in rows if keep(key)}


def asked_requests(con, run_id: str, repo: str, number: int, head: str) -> list:
    """``(title, body, state)`` of each issue one shadow run asked to file at this PR and head,
    but any that never will be (``denied``) (#2209); a copy dropped only on a possibly filed one
    (``MAYBE``) stands as that one does, as ``uncertain``."""
    return con.execute("SELECT title, body, CASE WHEN state='duplicate' AND error=? THEN "
                       "'uncertain' ELSE state END FROM shadow_issues WHERE run_id=? AND repo=? "
                       "AND pr=? AND head=? AND state!='denied' ORDER BY seq",
                       (MAYBE, run_id, repo, number, head)).fetchall()


class ShadowFilingRecords:
    """The filing methods of :class:`~diaktoros.run_supervisor.Supervisor`; ``_connect()`` is the
    one ``Supervisor`` provides."""

    def record_shadow_filing(self, run_id: str, repo: str, number: int, head: str, title: str,
                             depth: int, claim: tuple[int, int] | None = None,
                             same=None) -> int | None:
        """The ``filed_issues`` row of an issue the host files for a shadow review (#914), before
        the POST, so the origin-PR hold (#324) and the lineage bound (#487) see it as they see the
        reviewer's. Its sequence in that run; ``ValueError`` when this PR already filed the title
        (or one ``same`` takes for it), decided in the transaction that writes the row (review of
        #2585, F1): ``InFlight`` when only a POST still under way holds it, ``MaybeFiled`` when
        only one whose outcome is unknown does.
        ``claim`` (``(seq, attempts)`` of the ``shadow_issues`` row the caller claimed) fences it
        (review of #2585, F7): None, nothing written, unless that row is still ``posting`` under
        that claim, so a sweep resumed after ``settle_stale`` ended its claim never POSTs. A title
        found taken settles that claim here too (F8): back to ``recorded`` behind a POST under way,
        else a ``duplicate`` (``MAYBE`` behind an unknown), so a late refusal falls before or after."""
        key = " ".join(title.casefold().split())
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            if claim is not None and not con.execute(
                    "SELECT 1 FROM shadow_issues WHERE run_id=? AND seq=? AND attempts=? AND "
                    "state='posting'", (run_id, *claim)).fetchone():
                con.execute('ROLLBACK')
                return None
            kinds = _kinds(con, repo, number, same or (lambda other: other == key))
            if kinds:
                taken = (ValueError if 'filed' in kinds else MaybeFiled if 'maybe' in kinds
                         else InFlight)('an issue with this title was already filed from this PR')
                if claim is not None:       # the claim settles in this transaction (F8)
                    con.execute("UPDATE shadow_issues SET state=?,error=COALESCE(?,error),"
                                "updated=? WHERE run_id=? AND seq=?",
                                ('recorded' if type(taken) is InFlight else 'duplicate',
                                 MAYBE if type(taken) is MaybeFiled else None, time.time(),
                                 run_id, claim[0]))
                con.execute('COMMIT')
                raise taken
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

    def drop_copy(self, run_id: str, seq: int, decide) -> str | None:
        """Drop a ``recorded`` issue as a ``duplicate`` when ``decide(con)``, run in the
        transaction that writes it, still finds its copy ``"filed"`` or ``"maybe"`` (``MAYBE``),
        so a late refusal (``refused_after_claim``) lands wholly before that decision or after
        the drop it then undoes (review of #2585, F8). What ``decide`` found."""
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            kind = decide(con)
            if kind in ('filed', 'maybe'):
                con.execute("UPDATE shadow_issues SET state='duplicate',error=COALESCE(?,error),"
                            "updated=? WHERE run_id=? AND seq=? AND state='recorded'",
                            (MAYBE if kind == 'maybe' else None, time.time(), run_id, seq))
            con.execute('COMMIT')
            return kind

    def filed_standing(self, repo: str, number: int, keep) -> set[str]:
        """How the filings from a PR whose title key ``keep`` accepts stand (``_kinds``)."""
        with self._connect() as con:
            return _kinds(con, repo, number, keep)

    def refused_after_claim(self, run_id: str, seq: int, filing: int, attempts: int, reason: str,
                            limit: int) -> str:
        """A claimed issue's POST (``filing``) was refused with nothing written: release its
        ``filed_issues`` row and count the refusal (``recorded``, or ``denied`` at ``limit``), in
        one transaction. Also when ``settle_stale`` meanwhile ended that claim as ``uncertain``
        (review of #2585, F8): a definite refusal settles that unknown, so the row is released,
        the refusal counted, and each copy dropped only on such an unknown (``MAYBE``) taken up
        again. Only from the claim made with ``attempts``. The state it moved to, or ''."""
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute("SELECT i.state, i.repo, i.pr, f.state AS filed, f.error FROM "
                              "shadow_issues i LEFT JOIN filed_issues f ON f.run_id=i.run_id AND "
                              "f.seq=? WHERE i.run_id=? AND i.seq=? AND i.attempts=?",
                              (filing, run_id, seq, attempts)).fetchone()
            settled = (row is not None and row['state'] == 'uncertain'
                       and row['filed'] == 'uncertain' and row['error'] == STALE_WHY)
            if row is None or not (row['state'] == 'posting' or settled):
                con.execute('ROLLBACK')
                return ''
            con.execute("DELETE FROM filed_issues WHERE run_id=? AND seq=? AND state IN "
                        "('recorded','uncertain')", (run_id, filing))
            state = 'denied' if attempts + 1 >= limit else 'recorded'
            note = (f'not sent, dropped after {attempts + 1} refused attempts: {reason}'
                    if state == 'denied' else f'not sent, retried next sweep: {reason}')
            con.execute('UPDATE shadow_issues SET state=?,attempts=?,error=?,updated=? '
                        'WHERE run_id=? AND seq=?',
                        (state, attempts + 1, note[:200], time.time(), run_id, seq))
            if settled:
                con.execute("UPDATE shadow_issues SET state='recorded',error=NULL,updated=? "
                            "WHERE repo=? AND pr=? AND state='duplicate' AND error=?",
                            (time.time(), row['repo'], row['pr'], MAYBE))
            con.execute('COMMIT')
            return state

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
