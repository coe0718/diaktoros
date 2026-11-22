"""A maintainer's reply settles before the fix turn it woke starts (#2248).

A maintainer often comments "Fixed on main … Closing." and closes the issue seconds later. The
comment wakes the fixer (``fix_reply``, and an ask's resume in ``fix_hold.sweep_asks``); started
at once, the turn ran while the close landed, spent the model's tokens, and ended denied with a
notice to the operator for a close the maintainer made on purpose.

So a reply-woken turn is queued ``reply_settle_seconds`` ahead (``fix_hold.queue_fix`` with
``settle``; the ledger row waits on ``run_supervisor.SETTLE_HOLD``). When it falls due, the
worker's pre-launch read (``issue_inputs.issue_fix_issue``) ends a closed issue's turn as
``cancelled`` before any model runs (#723). A turn that ends because its issue closed, there or
at the broker's write, is logged here, never sent to the observer: the close is not news.
Other denials still notify. A turn queued any other way (a fix label, a held hand-off released)
starts at once, as before.
"""
from __future__ import annotations

from . import config
from .run_supervisor import issue_inputs
from .util import log


def seconds(loop: dict) -> int:
    """The loop's ``reply_settle_seconds`` (0 = off). ``normalize`` checked it; a value that is
    not a whole number of seconds >= 0 (a loop dict never normalized) reads as the default."""
    value = loop.get("reply_settle_seconds", config.DEFAULTS["reply_settle_seconds"])
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return config.DEFAULTS["reply_settle_seconds"]
    return value


def closed(error: object) -> bool:
    """Whether ``error`` (a run's or a denied write's recorded reason) is the issue's close."""
    return error == issue_inputs.ISSUE_CLOSED


def quiet_denial(number: int, outcome: str) -> bool:
    """For the broker's issue-fix notice: True (and one log line) when ``outcome`` is a write
    denied because the issue closed, which the observer is not told; else False."""
    if outcome != f"denied: {issue_inputs.ISSUE_CLOSED}":
        return False
    log(f"issue #{number}: fix turn's write denied, the issue was closed meanwhile; no notice")
    return True


def dropped_before_launch(row, error: object) -> None:
    """One log line for an issue-fix run the pre-launch read ended because its issue closed
    (``cancelled``, no model run, no notice). Any other run or reason logs nothing here."""
    if row is not None and row["seat"] == "issue_fixer" and closed(error):
        log(f"issue #{row['pr']}: fix turn ended before launch, the issue was closed; no model "
            "ran and no notice was sent")
