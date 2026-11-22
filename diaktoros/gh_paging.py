"""A keep-partial listing's later pages under a time budget (#1870, reviews on #2578).

A later page is read only inside a *window*: what is left of the budget less ``keep()``, the time
held back for the rest of the run (one call's cap, at most a quarter of the budget). For the read,
the budget's deadline *is* the window's end, so everything the page waits on is bounded by it, not
only its HTTP call: minting or waiting for an App token, signing, the lock another minter holds
(review on #2578, F1). A page that runs past its window ends the listing with the pages already
read (and a reason), not with ``GateBudgetExceeded``; only a run whose own budget is spent raises.
A window shorter than ``MIN_PAGE_S`` reads nothing more. A page that fails inside its window for
another reason is still a failed page: the caller reads the listing as unknown. Where the listing
stopped is the caller's to keep (``open_issues_read``'s ``progress``), so the next run can start
there instead of reading the same first pages again.
"""
from __future__ import annotations

import time

MIN_PAGE_S = 1.0
_SLACK_S = 0.05             # a call that failed this close to its deadline ran out of it


def keep() -> float:
    """Seconds held back for the rest of the run: one call's cap, at most a quarter of the budget."""
    from . import gh
    gate = gh._gate()
    per_call, budget = float(gate.get("per_call") or 30.0), gate.get("budget") or 0
    return min(per_call, budget / 4) if budget > 0 else per_call


def later_page(read, what: str, page: int):
    """``(items, error, '')`` from ``read()`` inside the page's window, or ``(None, None, why)``
    when the window is too short to start it or the read ran out of it."""
    from . import gh
    left = gh.remaining()
    if left is None:
        return (*read(), "")
    window = left - keep()
    if window < MIN_PAGE_S:
        return None, None, f"{what} page {page} not read: {max(left, 0):.0f}s of the budget left"
    gate = gh._gate()
    deadline = gate["deadline"]
    cap = min(float(gate.get("per_call") or 30.0), window)
    gate["deadline"] = time.monotonic() + cap       # the page's own deadline, for all it waits on
    why = ""
    try:
        items, error = read()
        if error and gh.remaining() <= _SLACK_S:
            why = error
    except gh.GateBudgetExceeded as exc:
        if deadline - time.monotonic() <= 0:        # the run's own budget, not the page's window
            raise
        why = str(exc)
    finally:
        gate["deadline"] = deadline
    if why:
        return None, None, (f"{what} page {page} not read within the {cap:.0f}s of the budget "
                            f"it could have ({why})")
    return items, error, ""
