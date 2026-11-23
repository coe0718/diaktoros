"""Where a budget-cut triage recovery listing resumes, and the check that it skipped nothing.

The open-issues listing is newest first by ``created`` and paged by offset, so a page number is
not a stable boundary. When an issue or PR newer than where a listing stopped closes before the
next read, every older item moves up one place, and the first items of the page it resumes at
move onto the page before it, which is not read again (review on #2578, F6). That happens
between sweeps, and between any two requests of one sweep.

So the sweep keeps the last item it read (number and ``created_at``) with the page
(``boundary``), and a resumed sweep (``resumed``) reads that page first, alone. The listing is
proven to skip nothing (``proven``) when the page reaches back to the item kept (it is on the
page, or the page's first item is newer than it, ``reaches_back``). Otherwise the sweep reads
again, straight after that page and before any later one, the ``SPAN`` places around the page's
start (``overlap``): one ``per_page=SPAN`` page, which for every page start the listing can
resume at holds 19 or more places before it and one or more after it (``window``). Then two
links must hold:

* those places reach back to the item kept, so every item after it up to them is among them;
* they share an item with the page, so whatever moved between the two requests is on one or the
  other. When the page is empty, they must have reached the listing's end themselves.

Reading the page first keeps the listing moving (F7): a fast page is never left without the
budget by the places read before it, and a page that reaches back needs no second request.

When either link fails (more closed than it reads again, nothing kept, or the list changed
between the requests), the items read are still checked, and the next sweep starts over from
page 1, which a listing always covers. So does a sweep the budget cuts before the places.
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


def _read(loop: dict, since: str, size: int, at: int, page: int) -> tuple[list | None, str]:
    """One ``per_page=size`` page (``at``) of ``gh.open_issues_read``'s listing, read inside the
    budget like any later page: ``(items, '')``, ``([], why)`` when the budget cut it, or
    ``(None, reason)`` when it could not be read."""
    path = (f"/repos/{loop['repo']}/issues?state=open&sort=created&direction=desc"
            f"&since={since}&per_page={size}&page={at}")
    what = "open issue" + ("" if size == gh.REVIEW_PAGE_SIZE else " places around")
    items, error, cut = gh_paging.later_page(lambda: gh.fetch(loop, path), what, page)
    if cut:
        return [], cut
    if error or not isinstance(items, list) or len(items) > size \
            or not all(isinstance(item, dict) for item in items):
        return None, f"{what} page {page}: {error or 'invalid list'}"
    return items, ""


def overlap(loop: dict, since: str, page: int) -> tuple[list | None, str]:
    """The ``SPAN`` places around ``page``'s start, as ``_read`` returns them."""
    return _read(loop, since, SPAN, window(page)[0], page)


def resumed(loop: dict, since: str, start: int, kept: dict,
            progress: dict) -> tuple[list | None, str, bool]:
    """The listing from page ``start`` (``start`` up to ``gh.MAX_ISSUE_PAGES``; past it, the
    probe ``gh.open_issues_read`` reads), with the places around it when the page does not reach
    back to ``kept``: ``(issues, error, proven)``. A cut before the places sets
    ``progress['next']`` to ``start``: the next sweep starts over."""
    if start > gh.MAX_ISSUE_PAGES:
        page, error = gh.open_issues_read(loop, since, start, progress)
    else:
        page, error = _read(loop, since, gh.REVIEW_PAGE_SIZE, start, start)
        if error and page is not None:
            progress["next"] = start
    if error or page is None or reaches_back(kept, page):
        head = []
    else:
        head, error = overlap(loop, since, start)
        if error:
            if head is not None:          # the places did not fit: the page alone proves nothing
                progress["next"] = start
            return (page if head is not None else None), error, False
    rest = []
    if not error and page is not None and start <= gh.MAX_ISSUE_PAGES \
            and not ends(page, since):
        rest, error = gh.open_issues_read(loop, since, start + 1, progress)
        if rest is None:
            return None, error, False
    issues = None if page is None else joined(head, page + rest)
    return issues, error, issues is not None and proven(kept, head, page)


def reaches_back(kept: dict, items: list) -> bool:
    """Whether ``items`` (the places read again) start at or before the item ``kept``: it is
    among them, or their first is newer than it. Ties in ``created_at`` prove nothing."""
    numbers = {item.get("number") for item in items}
    mark = epoch(kept.get("after")) if isinstance(kept.get("after"), str) else 0
    first = epoch(items[0].get("created_at")) if items else 0
    last = kept.get("last")
    return type(last) is int and bool(mark) and (last in numbers or bool(first) and first > mark)


def ends(page: list, since: str) -> bool:
    """Whether the listing ends at this page, as ``gh.open_issues_read`` reads it: a short page,
    or one whose last item was created before ``since``."""
    return len(page) < gh.REVIEW_PAGE_SIZE or gh._older_than(page[-1], epoch(since))


def proven(kept: dict, head: list, page: list) -> bool:
    """Whether a listing resumed at a page skipped nothing: the page (``page``) reaches back to
    the item ``kept``, or the places read again (``head``) do and share an item with the page."""
    if reaches_back(kept, page):
        return True
    numbers = {item.get("number") for item in page}
    meets = bool(numbers & {item.get("number") for item in head}) if page else len(head) < SPAN
    return reaches_back(kept, head) and meets


def joined(head: list, rest: list) -> list:
    """The places read again, then the pages read, each item once."""
    seen = {item.get("number") for item in head}
    return head + [item for item in rest if item.get("number") not in seen]
