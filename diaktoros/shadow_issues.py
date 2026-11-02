"""File the issues a shadow review asked for (#914).

The shadow seat's broker is no-write: a ``file_issue`` it asks for is recorded in the run ledger
(``shadow_issues``), never posted by the seat. The watchdog sweep calls :func:`file_pending`, which
files each recorded one once the real review exists at that head, unless the reviewer already filed
the same finding there (the same normalized title, or one close to it). It is filed by the reader,
not the reviewer, labelled ``shadow-found``, and says which model found it. Off unless
``shadow.file_issues`` is on; at most ``config.shadow_issues_daily(loop)`` a day (0 = no cap). An
unreadable review or ledger files nothing that sweep. A shadow issue whose head never gets a real
review is dropped after ``WAIT_S``.
"""
from __future__ import annotations

import difflib
import json
import re
import time

from . import broker, config, gate, gh

WAIT_S = 7 * 86400        # a head the real reviewer never reached is not waited on for ever
SIMILAR = 0.85            # titles this alike (normalized) are the same finding


def _same(title: str, filed: list[str]) -> bool:
    key = " ".join(title.casefold().split())
    return any(key == other or difflib.SequenceMatcher(None, key, other).ratio() >= SIMILAR
               for other in filed)


def file_pending(loop: dict, db=None, now: float | None = None) -> list[int]:
    """File what is due; the numbers of the issues filed this call."""
    from . import run_supervisor
    if not config.shadow_file_issues(loop):
        return []
    now = time.time() if now is None else now
    sup = run_supervisor.Supervisor(db or run_supervisor.production_ledger(), create=False)
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
                sup.shadow_issue_status(row["run_id"], row["seq"], "denied", expect="recorded",
                                        error="the real review never reached its head")
            continue                                  # the real review has not landed yet
        if _same(row["title"], sup.filed_titles(loop["repo"], number)):
            sup.shadow_issue_status(row["run_id"], row["seq"], "duplicate", expect="recorded")
            continue
        if room <= 0 and not uncapped:
            break
        # Lineage as the reviewer's filing has it (#487): one deeper than the issue this PR
        # fixed, read from its branch. An unreadable PR files nothing this sweep.
        pr = gh.pr(loop, number)
        branch = ((pr or {}).get("head") or {}).get("ref") if isinstance(pr, dict) else None
        if not isinstance(branch, str):
            continue
        match = re.fullmatch(config.ISSUE_FIX_BRANCH_RE, branch)
        depth = (sup.filed_depth(loop["repo"], int(match.group(1))) or 0) + 1 if match else 1
        # Claimed before the POST: a second sweep never files it twice; a crash after this
        # leaves it `posting`, which is never replayed.
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
        room -= 1
        try:
            issue = broker.file_shadow_issue(
                loop, repo=loop["repo"], number=number, head=head, title=row["title"],
                body=row["body"], labels=json.loads(row["labels"] or "[]"),
                model=sup.shadow_model(row["run_id"]) or "unknown", depth=depth)
        except Exception as exc:
            why = f"POST outcome unknown: {type(exc).__name__}: {exc}"[:200]
            sup.shadow_issue_status(row["run_id"], row["seq"], "uncertain", error=why)
            sup.filed_issue_status(row["run_id"], filing, "uncertain", error=why)
            continue
        sup.filed_issue_status(row["run_id"], filing, "posted", issue_number=issue)
        sup.shadow_issue_status(row["run_id"], row["seq"], "posted", issue_number=issue)
        filed.append(issue)
    return filed
