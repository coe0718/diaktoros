"""A keep-partial listing's later pages under a time budget (#1870, reviews on #2578).

A later page is read only inside a *window*: what is left of the budget less ``keep()``, the time
held back for the rest of the run (one call's cap, at most a quarter of the budget). The page's
call is capped at that window, so a slow page never spends what the rest of the run needs, and one
that runs past it ends the listing with the pages already read (and a reason), not with
``GateBudgetExceeded``. A window shorter than ``MIN_PAGE_S`` reads nothing more. A page that fails
inside its window for another reason is still a failed page: the caller reads the listing as
unknown. Where the listing stopped is the caller's to keep (``open_issues_read``'s ``progress``),
so the next run can start there instead of reading the same first pages again.
"""
from __future__ import annotations

import time

MIN_PAGE_S = 1.0


def keep() -> float:
    """Seconds held back for the rest of the run: one call's cap, at most a quarter of the budget."""
    from . import gh
    gate = gh._gate()
    per_call, budget = float(gate.get("per_call") or 30.0), gate.get("budget") or 0
    return min(per_call, budget / 4) if budget > 0 else per_call


def later_page(read, what: str, page: int):
    """``(items, error, '')`` from ``read()`` inside the page's window, or ``(None, None, why)``
    when the window is too short to start it or the call ran out of it."""
    from . import gh
    left = gh.remaining()
    if left is None:
        return (*read(), "")
    window = left - keep()
    if window < MIN_PAGE_S:
        return None, None, f"{what} page {page} not read: {max(left, 0):.0f}s of the budget left"
    gate = gh._gate()
    per_call = gate.get("per_call")
    cap = min(float(per_call or 30.0), window)
    gate["per_call"], started = cap, time.monotonic()
    try:
        items, error = read()
    finally:
        gate["per_call"] = per_call
    if error and time.monotonic() - started >= cap - 0.05:     # it ran out of its window
        return None, None, (f"{what} page {page} not read within the {cap:.0f}s of the budget "
                            f"it could have ({error})")
    return items, error, ""
