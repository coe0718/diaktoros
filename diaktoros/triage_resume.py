"""Where a budget-cut triage recovery listing resumes, and the check that it skipped nothing.

The open-issues listing is newest first by ``created`` and paged by offset, so a page number is
not a stable boundary. When an issue or PR newer than where a listing stopped closes before the
next read, every older item moves up one place, and the first items of the page it resumes at
move onto the page before it, which is not read again (review on #2578, F6). That happens
between sweeps, and between any two requests of one sweep.

So the sweep keeps the last item it read (number and ``created_at``) with the page
(``boundary``), and a resumed sweep first reads again the ``SPAN`` places around that page's
start (``overlap``): one ``per_page=SPAN`` page, which for every page start the listing can
resume at holds 19 or more places before it and one or more after it (``window``). The
resumed listing is proven to skip nothing (``proven``) when two links hold:

* those places reach back to the item kept: it is among them, or their first is newer than it,
  so every item after it up to the page's start is among them (``reaches_back``);
* they meet the page read next: one of their items past the page's start is on that page too,
  so whatever moved between the two requests is on one or the other. When that page is empty,
  they must have reached the listing's end themselves.

When either link fails (more closed than it reads again, nothing kept, or the list changed
between the requests), the items read are still checked, and the next sweep starts over from
page 1, which a listing always covers.
"""
from __future__ import annotations

from . import gh, gh_paging
from .util import epoch

# Places per read again. A page start P = 100j (j up to gh.MAX_ISSUE_PAGES) is never a multiple
# of 67, so the 67-place page holding P + 1 holds P too: 100j mod 67 is 19 or more places before.
SPAN = 67


def boundary(issues: list) -> dict:
    """The last item read with a number and a readable ``created_at``, kept with the page."""
    for item in reversed(issues):
        if isinstance(item, dict) and type(item.get("number")) is int \
                and epoch(item.get("created_at")):
            return {"last": item["number"], "after": item["created_at"]}
    return {}


def window(page: int) -> tuple[int, int]:
    """The ``per_page=SPAN`` page holding the places just before and after ``page``'s start,
    and how many of its places come before that start."""
    start = (page - 1) * gh.REVIEW_PAGE_SIZE
    return start // SPAN + 1, start % SPAN


def overlap(loop: dict, since: str, page: int) -> tuple[list | None, str]:
    """The ``SPAN`` places around ``page``'s start in ``gh.open_issues_read``'s listing, read
    inside the budget like any later page: ``(items, '')``, ``([], why)`` when the budget cut
    it, or ``(None, reason)`` when it could not be read."""
    path = (f"/repos/{loop['repo']}/issues?state=open&sort=created&direction=desc"
            f"&since={since}&per_page={SPAN}&page={window(page)[0]}")
    items, error, cut = gh_paging.later_page(lambda: gh.fetch(loop, path), "open issue", page)
    if cut:
        return [], cut
    if error or not isinstance(items, list) or len(items) > SPAN \
            or not all(isinstance(item, dict) for item in items):
        return None, f"open issue places around page {page}: {error or 'invalid list'}"
    return items, ""


def reaches_back(kept: dict, items: list) -> bool:
    """Whether ``items`` (the places read again) start at or before the item ``kept``: it is
    among them, or their first is newer than it. Ties in ``created_at`` prove nothing."""
    numbers = {item.get("number") for item in items}
    mark = epoch(kept.get("after")) if isinstance(kept.get("after"), str) else 0
    first = epoch(items[0].get("created_at")) if items else 0
    last = kept.get("last")
    return type(last) is int and bool(mark) and (last in numbers or bool(first) and first > mark)


def proven(kept: dict, items: list, page: int, rest: list) -> bool:
    """Whether a listing resumed at ``page`` skipped nothing: the places read again (``items``)
    reach back to the item ``kept`` and meet ``rest``, the pages read after them."""
    first = rest[:gh.REVIEW_PAGE_SIZE]
    after = {item.get("number") for item in items[window(page)[1]:]}
    meets = bool(after & {item.get("number") for item in first}) if first else len(items) < SPAN
    return reaches_back(kept, items) and meets


def joined(head: list, rest: list) -> list:
    """The places read again, then the pages read, each item once."""
    seen = {item.get("number") for item in head}
    return head + [item for item in rest if item.get("number") not in seen]
