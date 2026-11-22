"""File the issues a shadow review asked for (#914).

The shadow seat's broker is no-write: a ``file_issue`` it asks for is recorded in the run ledger
(``shadow_issues``), never posted by the seat. The watchdog sweep calls :func:`file_pending`, which
files each recorded one once the real review exists at that head, unless the reviewer already filed
the same finding there (the same normalized title, or one close to it). It is filed by the reader,
not the reviewer, labelled ``shadow-found``, and says which model found it. Off unless
``shadow.file_issues`` is on; at most ``config.shadow_issues_daily(loop)`` a day (0 = no cap). An
unreadable review or ledger files nothing that sweep. A shadow issue whose head never gets a real
review is dropped after ``WAIT_S``. A POST refused with nothing written (``broker.BrokerNotSent``: no
reader identity, a reader credential that is missing or unreadable, a repository mismatch, a 4xx
other than 408 from GitHub) leaves the issue ``recorded`` with the reason, and the next sweep tries
again, up to ``MAX_ATTEMPTS`` refusals, after
which it is ``denied`` with the reason (#1732); any other failure of the POST (a 408 included) is
``uncertain``, never replayed. A claim a sweep was stopped after (left ``posting`` past
``POSTING_STALE_S``) is ended by :func:`settle_stale`, so nothing waits on it for ever (#2585).
Every one that ends ``uncertain`` or ``denied`` is said in the sweep's log, with its reason (#1359).
"""
from __future__ import annotations

import difflib
import json
import re
import time

from . import broker, config, gate, gh
from .broker import filing as broker_filing
from .ruled_approval import POSTING_STALE_S

WAIT_S = 7 * 86400        # a head the real reviewer never reached is not waited on for ever
MAX_ATTEMPTS = 12         # refused POSTs (nothing written) before the issue is dropped (#1732)
SIMILAR = 0.85            # titles this alike (normalized) are the same finding


def _same(title: str, filed: list[str]) -> bool:
    key = " ".join(title.casefold().split())
    return any(key == other or difflib.SequenceMatcher(None, key, other).ratio() >= SIMILAR
               for other in filed)


def file_pending(loop: dict, db=None, now: float | None = None,
                 log=lambda _msg: None) -> list[int]:
    """File what is due; the numbers of the issues filed this call. An issue GitHub filed
    whose labels are not confirmed (#1589: not applied, or the label write got no answer) is
    ``unlabelled``, with the gap recorded and logged."""
    from . import run_supervisor
    if not config.shadow_file_issues(loop):
        return []
    now = time.time() if now is None else now
    sup = run_supervisor.Supervisor(db or run_supervisor.production_ledger(), create=False)
    settle_stale(sup, loop["repo"], now, log)
    waiting = sup.shadow_issues_waiting(loop["repo"])
    if not waiting:
        return []
    room = (config.shadow_issues_daily(loop)
            - sup.shadow_issues_posted_since(loop["repo"], now - 86400))
    uncapped = config.shadow_issues_daily(loop) == 0
    reviews_of: dict[int, object] = {}
    filed = []
    for row in waiting:
        number, head = int(row["pr"]), str(row["head"])
        if number not in reviews_of:
            reviews_of[number] = gh.reviews(loop, number)
        reviews = reviews_of[number]
        if not isinstance(reviews, list):
            continue                                  # unknown: nothing filed this sweep
        if gate.latest_effective_review_at_head(reviews, loop, head) is None:
            if now - float(row["created"]) > WAIT_S:
                _lost(sup, row, "denied", "the real review never reached its head", log,
                      expect="recorded")
            continue                                  # the real review has not landed yet
        if _same(row["title"], sup.filed_titles(loop["repo"], number)):
            sup.shadow_issue_status(row["run_id"], row["seq"], "duplicate", expect="recorded")
            continue
        if room <= 0 and not uncapped:
            continue                                  # no room: later rows may still be lost
        depth = _lineage_depth(loop, sup, number)
        if depth is None:
            continue                                  # an unreadable PR files nothing this sweep
        # Claimed before the POST: a second sweep never files it twice; a crash after this
        # leaves it `posting`, which is never replayed, only ended by `settle_stale`.
        if not sup.shadow_issue_status(row["run_id"], row["seq"], "posting", expect="recorded"):
            continue
        # Its filed_issues row too, before the POST, so the origin-PR hold (#324) and the
        # lineage bound see a shadow-found issue exactly as they see the reviewer's.
        try:
            filing = sup.record_shadow_filing(row["run_id"], loop["repo"], number, head,
                                              row["title"], depth)
        except ValueError:
            sup.shadow_issue_status(row["run_id"], row["seq"], "duplicate")
            continue
        issue, sent = _post(loop, sup, row, filing, depth, log)
        if sent:
            room -= 1                                 # a POST that was never sent takes no room
        if issue is not None:
            filed.append(issue)
    return filed


def settle_stale(sup, repo: str, now: float, log) -> None:
    """End each claim (``posting``, a shadow's or an adjudicator's) left past ``POSTING_STALE_S``
    (#2585): its sweep was stopped after claiming it, and nothing else moves it, so a ruling's
    copy that waits on it would wait for ever. Read from its ``filed_issues`` row, which is
    committed before the POST and only then: none, the POST was never sent, so it goes back to
    ``recorded`` as a refusal does (counted, ``denied`` at ``MAX_ATTEMPTS``) and is filed later;
    that row ``posted``, it was filed (its labels unconfirmed: ``unlabelled``); otherwise the POST
    may have been sent, so it is ``uncertain``, never replayed, as ``ruled_approval`` does for a
    stale write (``POSTING_STALE_S``). Only a claim no POST can have left is retried, and the
    retry takes a fresh ``filed_issues`` row, whose unique title holds it to one POST."""
    with sup._connect() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            "SELECT i.run_id, i.seq, i.pr, i.title, i.attempts, r.seat, f.seq AS filing, "
            "f.state AS filed, f.issue_number FROM shadow_issues i JOIN runs r ON r.id=i.run_id "
            "LEFT JOIN filed_issues f ON f.run_id=i.run_id AND f.repo=i.repo AND f.pr=i.pr AND "
            "f.title_key=i.title_key WHERE i.repo=? AND i.state='posting' AND i.updated<?",
            (repo, now - POSTING_STALE_S)).fetchall()
        lost = []
        for row in rows:
            if row["filing"] is None:
                attempts = row["attempts"] + 1
                state = "denied" if attempts >= MAX_ATTEMPTS else "recorded"
                why = f"not sent: interrupted before the POST ({attempts} attempts)"
            elif row["filed"] == "posted":
                attempts, state, why = row["attempts"], "unlabelled", "labels: not confirmed"
            else:
                attempts, state = row["attempts"], "uncertain"
                why = "interrupted after the POST was claimed; outcome unknown"
                con.execute("UPDATE filed_issues SET state='uncertain', error=?, updated=? WHERE "
                            "run_id=? AND seq=? AND state='recorded'",
                            (why, time.time(), row["run_id"], row["filing"]))
            con.execute("UPDATE shadow_issues SET state=?, attempts=?, error=?, issue_number="
                        "COALESCE(?, issue_number), updated=? WHERE run_id=? AND seq=?",
                        (state, attempts, why, row["issue_number"], time.time(), row["run_id"],
                         row["seq"]))
            lost += [(row, state, why)] if state != "recorded" else []
        con.execute("COMMIT")
    for row, state, why in lost:
        what = "adjudicator-found" if row["seat"] == "adjudicator" else "shadow-found"
        log(f"{what} issue for #{int(row['pr'])} ({row['title']!r}) {state}: {why}")


def _lost(sup, row, state: str, why: str, log, what: str = "shadow-found",
          expect: str | None = None) -> None:
    """Record one issue as never to be filed (``denied``) or never replayed (``uncertain``) and
    say so in the sweep's log, so an operator sees a lost filing, not only the filed ones (#1359).
    With ``expect``, only from that state, and logged only when it moved."""
    if sup.shadow_issue_status(row["run_id"], row["seq"], state, expect=expect, error=why):
        log(f"{what} issue for #{int(row['pr'])} ({row['title']!r}) {state}: {why}")


def _lineage_depth(loop: dict, sup, number: int) -> int | None:
    """Lineage as the reviewer's filing has it (#487): one deeper than the issue this PR fixed,
    read from its branch. None when the PR is unreadable."""
    pr = gh.pr(loop, number)
    branch = ((pr or {}).get("head") or {}).get("ref") if isinstance(pr, dict) else None
    if not isinstance(branch, str):
        return None
    match = re.fullmatch(config.ISSUE_FIX_BRANCH_RE, branch)
    return (sup.filed_depth(loop["repo"], int(match.group(1))) or 0) + 1 if match else 1


def _send_shadow(loop: dict, sup, row, depth: int) -> tuple[int, str]:
    return broker_filing.file_shadow_issue(
        loop, repo=loop["repo"], number=int(row["pr"]), head=str(row["head"]),
        title=row["title"], body=row["body"], labels=json.loads(row["labels"] or "[]"),
        model=sup.shadow_model(row["run_id"]) or "unknown", depth=depth)


def _post(loop: dict, sup, row, filing, depth: int, log, send=_send_shadow,
          what: str = "shadow-found") -> tuple[int | None, bool]:
    """POST one claimed issue with ``send`` (a shadow's, or an adjudicator's: #1864) and record
    how it went: ``(issue, sent)``, the issue number when it was filed (None otherwise), and
    whether the POST may have reached GitHub."""
    number = int(row["pr"])
    try:
        issue, gap = send(loop, sup, row, depth)
    except broker.BrokerNotSent as exc:
        # Refused with nothing written (no reader identity, a repository mismatch, GitHub's
        # 4xx other than 408): not an unknown outcome. Back to `recorded` with the reason, so a
        # sweep after the operator fixes it files the issue; the filed_issues row is released for
        # that retry.
        # Each refusal is counted: at MAX_ATTEMPTS the issue is dropped, not retried for ever.
        sup.release_shadow_filing(row["run_id"], filing)
        state = sup.shadow_issue_refused(row["run_id"], row["seq"], str(exc), MAX_ATTEMPTS)
        log(f"{what} issue for #{number} ({row['title']!r}) not filed, "
            + ("dropped" if state == "denied" else "retried next sweep") + f": {exc}")
        return None, False
    except Exception as exc:
        why = f"POST outcome unknown: {type(exc).__name__}: {exc}"[:200]
        _lost(sup, row, "uncertain", why, log, what)
        sup.filed_issue_status(row["run_id"], filing, "uncertain", error=why)
        return None, True
    sup.filed_issue_status(row["run_id"], filing, "posted", issue_number=issue)
    # Filed either way (the hold and lineage read filed_issues); a shadow row whose labels
    # are not confirmed (not applied, or the write got no answer) is ``unlabelled`` (#1589).
    sup.shadow_issue_status(row["run_id"], row["seq"], "unlabelled" if gap else "posted",
                            issue_number=issue, error=f"labels: {gap}"[:200] if gap else None)
    if gap:
        log(f"{what} issue #{issue} filed without its labels: {gap}")
    return issue, True
