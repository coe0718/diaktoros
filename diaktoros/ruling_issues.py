"""File the follow-ups an adjudicator ruled valid but not blocking (#1864).

The adjudicator writes nothing but its ruling. With ``adjudicator_file_issues`` on, its ruling turn
may also ask, through the broker, to file up to ``broker_filing.FILED_ISSUES_MAX`` issues: each checked as
the reviewer's would be and recorded in the run ledger (``shadow_issues``, under the adjudicator's
run), never posted by the seat. The watchdog sweep calls :func:`file_pending`, which files each one
whose run recorded its ruling, as the reader, labelled ``adjudicator-found``, with a header naming
the PR it follows up and the ruling. Its ``filed_issues`` row makes ``fix_hold`` hold it for that PR
to merge, and counts its lineage, exactly as for the reviewer's and the shadow's.

Not filed: a title the reviewer or a shadow review already filed from the PR (or one close to it:
``duplicate``; while a shadow's or an adjudicator's such POST is still under way it is waited on,
since it may yet be refused: review of #2585, F1); one the shadow review it ruled over already
asked for at that PR and head: a title close to it, or a body whose first ``path:line`` (or
``path:line-line``) is in the same file with overlapping lines (``duplicate``, #2209) once that
request was filed or may have been, whatever the loop's settings now, while one still to be filed
holds the ruling's copy until it is (a denied one, refused or expired, or one recorded while the
loop no longer files a shadow's requests, lets the ruling's be filed; a claim stopped mid-POST is
ended by ``shadow_issues.settle_stale``); one whose run ended without a ruling (``denied``); any
past ``config.ADJUDICATOR_ISSUES_DAILY`` a day, which wait for a later sweep (a full bound stops
only the POSTs: the sweep still rules out the rest, #1982). A POST refused with nothing written is
retried, up to ``shadow_issues.MAX_ATTEMPTS``; any other failure is ``uncertain`` and never
replayed (the same rules as the shadow's, #1732). Each one that ends ``uncertain`` or ``denied`` is
said in the sweep's log (#1359).
"""
from __future__ import annotations

import json
import re
import time

from . import config, shadow_issues
from .broker import filing as broker_filing
from .run_supervisor import shadow_filings

# The first `path:line` or `path:line-line` a body cites (#2209), extensionless paths such as
# `Makefile:5` included. Never one inside a URL: each whole URL, its query string with it, is
# blanked out first; a last component of digits alone (`12:30`) names no file, nor does a line
# number (or range end) past nine digits, which ``int`` may refuse (review of #2585, F10).
_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I)
_CITE = re.compile(r"(?<![\w./:-])((?:[\w.-]+/)*[\w-][\w.-]*):(\d{1,9})(?:-(\d{1,9}))?(?!-?\d)")


def _cited(body) -> tuple[str, int, int] | None:
    match = next((m for m in _CITE.finditer(_URL.sub(" ", str(body or "")))
                  if re.search(r"[A-Za-z_]", m.group(1).rsplit("/", 1)[-1])), None)
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


# Each state a shadow request can be in (``shadow_issues``), for a ruling's copy of the same
# finding (#2209, review of #2585). Filed, whatever the loop's settings now: the shadow's own issue
# posted (``unlabelled``: posted, its labels unconfirmed), or ``duplicate`` (the reviewer's or
# another shadow's copy filed). Maybe filed: ``uncertain`` (its POST may have landed: never
# replayed, but a late, definite refusal shows it did not, F8), and a copy dropped only on such a
# one (``MAYBE``, which ``asked_requests`` reads as ``uncertain``): a ruling's copy is recorded and
# dropped beside it, and taken up again if that refusal comes (F9). Still to be filed:
# ``recorded``, only while the loop files a shadow's requests (off, no sweep ever files it), and
# ``posting``, a POST under way, whatever the setting, since it may land; a claim its sweep was
# stopped after is ended by ``shadow_issues.settle_stale``, so that wait is bounded. ``denied``
# never will be.
_FILED = frozenset({"posted", "unlabelled", "duplicate"})


def _standing(state: str, shadow_files: bool) -> str | None:  # also what the prompt lists
    """``"filed"``, ``"maybe"``, ``"waiting"`` or None (never to be filed) for a shadow
    request's state, as ``asked_requests`` reads it."""
    if state in _FILED:
        return "filed"
    if state == "uncertain":
        return "maybe"
    return "waiting" if state == "posting" or state == "recorded" and shadow_files else None


def _shadow_asked(loop: dict, sup, row, con=None) -> str | None:
    """Whether the shadow review this ruling was over asked, at its PR and head, for the same
    finding (#2209): a close title, or the same place cited first in both bodies. ``"filed"``
    when such a request was filed, whether or not the loop still files a shadow's requests;
    ``"maybe"`` when it may have been (dropped, but taken up again if a late refusal shows it was
    not: F8); ``"waiting"`` when the only ones are still to be filed (``_standing``), so the
    ruling's copy waits for their outcome rather than being dropped first. ``None`` for none, or
    none that will ever be filed. Read in ``con``'s transaction when given (F8)."""
    shadow = row["shadow_run"]
    if not shadow:
        return None
    place, files = _cited(row["body"]), config.shadow_file_issues(loop)
    where = (shadow, loop["repo"], int(row["pr"]), str(row["head"]))
    asked = shadow_filings.asked_requests(con, *where) if con else sup.shadow_requests(*where)
    kinds = {_standing(state, files) for title, body, state in asked
             if shadow_issues._same(row["title"], [" ".join(str(title).casefold().split())])
             or _same_place(place, _cited(body))}
    return next((kind for kind in ("filed", "maybe", "waiting") if kind in kinds), None)


_RULED = {"posted": "filed", "unlabelled": "filed", "uncertain": "maybe", "posting": "flying"}
_AS_CLAIM = {"filed": "filed", "maybe": "maybe", "waiting": "flying"}  # _shadow_asked, at a claim


def ruled_copy(sup, row, con=None) -> str | None:
    """The other way round (#2603): whether a ruling over this shadow request's run asked, at its
    PR and head, for the same finding (a close title, or the same place cited first in both
    bodies) and filed it, as its fallback while the loop did not file a shadow's requests:
    ``"filed"``, ``"maybe"`` (its POST's outcome unknown), ``"flying"`` (its POST under way) or
    None. A ruling's copy still to be filed waits on the shadow's, so it never holds it. Read in
    ``con``'s transaction when given."""
    if con is None:
        with sup._connect() as con:
            return ruled_copy(sup, row, con)
    place = _cited(row["body"])
    asked = shadow_filings.ruled_requests(con, row["run_id"], row["repo"], int(row["pr"]),
                                          str(row["head"]))
    kinds = {_RULED[state] for title, body, state in asked
             if shadow_issues._same(row["title"], [" ".join(str(title).casefold().split())])
             or _same_place(place, _cited(body))}
    return next((kind for kind in ("filed", "maybe", "flying") if kind in kinds), None)


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
    # Here too, not only in the shadow's sweep: a shadow claim stopped mid-POST must end even
    # while the loop no longer files a shadow's requests, or a ruling's copy waits on it for ever.
    shadow_issues.settle_stale(sup, loop["repo"], now, log)
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
        # Dropped only on a copy that was filed (or may have been); one still to be filed or under
        # way may yet be refused, so this one waits for it (review of #2585, F1).
        asked = _shadow_asked(loop, sup, row)
        filed_as = shadow_issues._filed(sup, loop["repo"], number, row["title"])
        known = {asked, filed_as}
        if known & {"filed", "maybe"}:
            shadow_issues._drop(sup, row, number, lambda con: _shadow_asked(loop, sup, row, con=con))
            continue
        if asked == "waiting" or filed_as == "flying":
            continue          # filed only if the shadow's copy never is (denied, filing off)
        if room <= 0:
            continue                                  # no room: later rows may still be lost
        depth = shadow_issues._lineage_depth(loop, sup, number)
        if depth is None:
            continue                                  # an unreadable PR files nothing this sweep
        # Rechecked as the claim is written (#2603 review F1): a shadow copy claimed since holds it.
        filing = shadow_issues._claim(sup, row, number, head, depth, log, "adjudicator-found",
                                      copy=lambda con: _AS_CLAIM.get(_shadow_asked(loop, sup, row,
                                                                                   con=con)))
        if filing is None:
            continue
        issue, sent = shadow_issues._post(loop, sup, row, filing, depth, log, send=_send,
                                          what="adjudicator-found")
        if sent:
            room -= 1                                 # a POST never sent takes no room
        if issue is not None:
            filed.append(issue)
    return filed
