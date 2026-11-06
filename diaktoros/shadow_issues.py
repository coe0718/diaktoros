"""File the issues a shadow review asked for (#914).

The shadow seat's broker is no-write: a ``file_issue`` it asks for is recorded in the run ledger
(``shadow_issues``), never posted by the seat. The watchdog sweep calls :func:`file_pending`, which
files each recorded one once the real review exists at that head, unless the reviewer already filed
the same finding there (the same normalized title, or one close to it). It is filed by the reader,
not the reviewer, labelled ``shadow-found``, and says which model found it. Off unless
``shadow.file_issues`` is on; at most ``config.shadow_issues_daily(loop)`` a day (0 = no cap). An
unreadable review or ledger files nothing that sweep. A shadow issue whose head never gets a real
review is dropped after ``WAIT_S``. A POST refused with nothing written (``broker.BrokerNotSent``: no
reader identity, a repository mismatch, a 4xx from GitHub) leaves the issue ``recorded`` with the
reason, and the next sweep tries again, up to ``MAX_ATTEMPTS`` refusals, after which it is
``denied`` with the reason (#1732); any other failure of the POST is ``uncertain``, never replayed.
Every one that ends ``uncertain`` or ``denied`` is said in the sweep's log, with its reason (#1359).
"""
from __future__ import annotations

import difflib
import json
import re
import time

from . import broker, config, gate, gh

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
    without some of its labels (#1589) is ``unlabelled``, with the gap recorded and logged."""
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
                _lost(sup, row, "denied", "the real review never reached its head", log,
                      expect="recorded")
            continue                                  # the real review has not landed yet
        if _same(row["title"], sup.filed_titles(loop["repo"], number)):
            sup.shadow_issue_status(row["run_id"], row["seq"], "duplicate", expect="recorded")
            continue
        if room <= 0 and not uncapped:
            break
        depth = _lineage_depth(loop, sup, number)
        if depth is None:
            continue                                  # an unreadable PR files nothing this sweep
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
        issue, sent = _post(loop, sup, row, filing, depth, log)
        if sent:
            room -= 1                                 # a POST that was never sent takes no room
        if issue is not None:
            filed.append(issue)
    return filed


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
    return broker.file_shadow_issue(
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
        # 4xx): not an unknown outcome. Back to `recorded` with the reason, so a sweep after the
        # operator fixes it files the issue; the filed_issues row is released for that retry.
        # Each refusal is counted: at MAX_ATTEMPTS the issue is dropped, not retried for ever.
        sup.release_shadow_filing(row["run_id"], filing)
        state = sup.shadow_issue_refused(row["run_id"], row["seq"], str(exc), MAX_ATTEMPTS)
        log(f"{what} issue for #{number} not filed, "
            + ("dropped" if state == "denied" else "retried next sweep") + f": {exc}")
        return None, False
    except Exception as exc:
        why = f"POST outcome unknown: {type(exc).__name__}: {exc}"[:200]
        _lost(sup, row, "uncertain", why, log, what)
        sup.filed_issue_status(row["run_id"], filing, "uncertain", error=why)
        return None, True
    sup.filed_issue_status(row["run_id"], filing, "posted", issue_number=issue)
    # Filed either way (the hold and lineage read filed_issues); a shadow row whose labels
    # did not land is ``unlabelled``, not ``posted`` (#1589).
    sup.shadow_issue_status(row["run_id"], row["seq"], "unlabelled" if gap else "posted",
                            issue_number=issue, error=f"labels: {gap}"[:200] if gap else None)
    if gap:
        log(f"{what} issue #{issue} filed without its labels: {gap}")
    return issue, True
