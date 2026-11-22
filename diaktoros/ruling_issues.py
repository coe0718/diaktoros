"""File the follow-ups an adjudicator ruled valid but not blocking (#1864).

The adjudicator writes nothing but its ruling. With ``adjudicator_file_issues`` on, its ruling turn
may also ask, through the broker, to file up to ``broker_filing.FILED_ISSUES_MAX`` issues: each checked as
the reviewer's would be and recorded in the run ledger (``shadow_issues``, under the adjudicator's
run), never posted by the seat. The watchdog sweep calls :func:`file_pending`, which files each one
whose run recorded its ruling, as the reader, labelled ``adjudicator-found``, with a header naming
the PR it follows up and the ruling. Its ``filed_issues`` row makes ``fix_hold`` hold it for that PR
to merge, and counts its lineage, exactly as for the reviewer's and the shadow's.

Not filed: a title the reviewer or a shadow review already filed from the PR (or one close to it:
``duplicate``); one the shadow review it ruled over already asked for at that PR and head, where
the loop files a shadow's requests: a title close to it, or a body whose first ``path:line`` (or
``path:line-line``) is in the same file with overlapping lines (``duplicate``, #2209); one whose run
ended without a ruling (``denied``); any past
``config.ADJUDICATOR_ISSUES_DAILY`` a day, which wait for a later sweep (a full bound stops only the
POSTs: the sweep still rules out the rest, #1982). A POST refused with nothing
written is retried, up to ``shadow_issues.MAX_ATTEMPTS``; any other failure is ``uncertain`` and
never replayed (the same rules as the shadow's, #1732). Each one that ends ``uncertain`` or
``denied`` is said in the sweep's log (#1359).
"""
from __future__ import annotations

import json
import re
import time

from . import config, shadow_issues
from .broker import filing as broker_filing

# The first `path:line` or `path:line-line` a body cites; never one inside a URL (#2209).
_CITE = re.compile(r"(?<![\w./:-])((?:[\w.-]+/)*[\w-][\w.-]*\.\w+):(\d+)(?:-(\d+))?(?!\d)")


def _cited(body) -> tuple[str, int, int] | None:
    match = _CITE.search(str(body or ""))
    if match is None:
        return None
    first, last = int(match.group(2)), int(match.group(3) or match.group(2))
    return match.group(1), min(first, last), max(first, last)


def _same_place(one, other) -> bool:
    """The same file (one path may name it from deeper in the tree) and overlapping lines."""
    if one is None or other is None:
        return False
    (a, a1, a2), (b, b1, b2) = one, other
    return ((a == b or a.endswith("/" + b) or b.endswith("/" + a))
            and a1 <= b2 and b1 <= a2)


def _shadow_asked(loop: dict, sup, row) -> bool:
    """Whether the shadow review this ruling was over asked, at its PR and head, for the same
    finding (#2209): a close title, or the same place cited first in both bodies. Only where the
    loop files a shadow's requests, since otherwise they are never filed."""
    shadow = row["shadow_run"]
    if not shadow or not config.shadow_file_issues(loop):
        return False
    place = _cited(row["body"])
    return any(shadow_issues._same(row["title"], [" ".join(str(title).casefold().split())])
               or _same_place(place, _cited(body))
               for title, body in sup.shadow_requests(shadow, loop["repo"], int(row["pr"]),
                                                      str(row["head"])))


def _send(loop: dict, sup, row, depth: int) -> tuple[int, str]:
    return broker_filing.file_ruling_issue(
        loop, repo=loop["repo"], number=int(row["pr"]), head=str(row["head"]),
        title=row["title"], body=row["body"], labels=json.loads(row["labels"] or "[]"),
        verdict=str(row["verdict"]), depth=depth)


def file_pending(loop: dict, db=None, now: float | None = None,
                 log=lambda _msg: None) -> list[int]:
    """File what is due; the numbers of the issues filed this call."""
    from . import run_supervisor
    if not config.adjudicator_file_issues(loop):
        return []
    now = time.time() if now is None else now
    sup = run_supervisor.Supervisor(db or run_supervisor.production_ledger(), create=False)
    waiting = sup.shadow_issues_waiting(loop["repo"], seat="adjudicator")
    if not waiting:
        return []
    room = (config.ADJUDICATOR_ISSUES_DAILY
            - sup.shadow_issues_posted_since(loop["repo"], now - 86400, seat="adjudicator"))
    filed = []
    for row in waiting:
        number, head = int(row["pr"]), str(row["head"])
        if row["verdict"] is None:
            if row["run_state"] not in run_supervisor.NOT_ENDED:
                shadow_issues._lost(sup, row, "denied",
                                    "the adjudicator's run ended without a ruling", log,
                                    what="adjudicator-found", expect="recorded")
            continue                                  # still ruling: filed once it has
        if (shadow_issues._same(row["title"], sup.filed_titles(loop["repo"], number))
                or _shadow_asked(loop, sup, row)):
            sup.shadow_issue_status(row["run_id"], row["seq"], "duplicate", expect="recorded")
            continue
        if room <= 0:
            continue                                  # no room: later rows may still be lost
        depth = shadow_issues._lineage_depth(loop, sup, number)
        if depth is None:
            continue                                  # an unreadable PR files nothing this sweep
        # Claimed before the POST, so a second sweep never files it twice; then its filed_issues
        # row, so the origin-PR hold (#324) and the lineage bound see it (#487).
        if not sup.shadow_issue_status(row["run_id"], row["seq"], "posting", expect="recorded"):
            continue
        try:
            filing = sup.record_shadow_filing(row["run_id"], loop["repo"], number, head,
                                              row["title"], depth)
        except ValueError:
            sup.shadow_issue_status(row["run_id"], row["seq"], "duplicate")
            continue
        issue, sent = shadow_issues._post(loop, sup, row, filing, depth, log, send=_send,
                                          what="adjudicator-found")
        if sent:
            room -= 1                                 # a POST never sent takes no room
        if issue is not None:
            filed.append(issue)
    return filed
