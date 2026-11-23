"""Where a budget-cut triage recovery listing resumes, and the check that it skipped nothing.

The open-issues listing is newest first by ``created`` and paged by offset, so a page number is
not a stable boundary. When an issue or PR newer than where a listing stopped closes before the
next sweep, every older item moves up one place, and the first items of the page it resumes at
move onto the page before it, which that sweep does not read again (review on #2578, F6). So the
sweep keeps the last item it read (number and ``created_at``) with the page (``boundary``), and a
resumed sweep first reads the last ``OVERLAP`` places before that page again (``overlap``). They
start at or before the kept item when fewer than ``OVERLAP`` of the items before it closed: the
kept item is among them, or their first is newer than it (``reaches_back``). Every item after it
is then among them or on the pages read next. When they do not, the pages read are still
checked, and the next sweep starts over from page 1, which a listing always covers.
"""
from __future__ import annotations

from . import gh, gh_paging
from .util import epoch

OVERLAP = 25                # places read again before the page a listing resumes at; divides 100


def boundary(issues: list) -> dict:
    """The last item read with a number and a readable ``created_at``, kept with the page."""
    for item in reversed(issues):
        if isinstance(item, dict) and type(item.get("number")) is int \
                and epoch(item.get("created_at")):
            return {"last": item["number"], "after": item["created_at"]}
    return {}


def overlap(loop: dict, since: str, page: int) -> tuple[list | None, str]:
    """The last ``OVERLAP`` places before ``page`` of ``gh.open_issues_read``'s listing, read
    inside the budget like any later page: ``(items, '')``, ``([], why)`` when the budget cut
    it, or ``(None, reason)`` when it could not be read."""
    at = (page - 1) * gh.REVIEW_PAGE_SIZE // OVERLAP
    path = (f"/repos/{loop['repo']}/issues?state=open&sort=created&direction=desc"
            f"&since={since}&per_page={OVERLAP}&page={at}")
    items, error, cut = gh_paging.later_page(lambda: gh.fetch(loop, path), "open issue", page)
    if cut:
        return [], cut
    if error or not isinstance(items, list) or len(items) > OVERLAP \
            or not all(isinstance(item, dict) for item in items):
        return None, f"open issue places before page {page}: {error or 'invalid list'}"
    return items, ""


def reaches_back(kept: dict, items: list) -> bool:
    """Whether ``items`` (the places read again) start at or before the item ``kept``: it is
    among them, or their first is newer than it. Ties in ``created_at`` prove nothing."""
    numbers = {item.get("number") for item in items}
    mark = epoch(kept.get("after")) if isinstance(kept.get("after"), str) else 0
    first = epoch(items[0].get("created_at")) if items else 0
    last = kept.get("last")
    return type(last) is int and bool(mark) and (last in numbers or bool(first) and first > mark)


def joined(head: list, rest: list) -> list:
    """The places read again, then the pages read, each item once."""
    seen = {item.get("number") for item in head}
    return head + [item for item in rest if item.get("number") not in seen]
