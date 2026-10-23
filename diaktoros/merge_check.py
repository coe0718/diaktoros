"""The ``diaktoros/review`` merge check's state (#1163 M2b): what the loop says about a PR head,
as GitHub's merge button understands it.

``ready`` already decides whether a head may merge; this turns its reasons (``ready.assess_kinds``)
into one check state. Posting it is separate (a check run from the reviewer App, or a commit status
from the reviewer's PAT): this module reads and decides, it writes nothing.

* **success**: nothing the loop knows holds the head.
* **failure**: something needs a change or a person — changes requested at the head, red required
  CI, a conflict, a stale approval, an open gate failure, a human-paths hold, the shadow's hold or a
  ruling the operator decides.
* **pending**: everything left can still clear on its own — a review, the shadow, CI or a ruling
  still out, a draft, or a read that failed and will be retried.

GitHub's ``blocked`` never counts: with this check required, GitHub reports ``blocked`` *because*
the check is not green yet, and counting that would keep the check from ever turning green. (Its
other verdicts do: a conflict or ``behind`` needs a change, an uncomputed state is pending.) The
per-PR reasons come from ``ready.build_kinds``, stacking included. A PR outside the loop gets no
check at all.
"""
from __future__ import annotations

from . import ci, ready

NAME = ci.MERGE_CHECK
SUCCESS, PENDING, FAILURE = "success", "pending", "failure"
SUMMARY_MAX = 60_000          # GitHub's check-run summary allows 65535 characters


def decide(kinds: list[tuple[str, str]]) -> tuple[str | None, str]:
    """``(state, summary)`` from ``ready.assess_kinds``' reasons; state ``None`` when the PR is not
    the loop's (no check is posted)."""
    if any(kind == ready.OUTSIDE for kind, _ in kinds):
        return None, "not a loop PR"
    counted = [(kind, text) for kind, text in kinds if kind != ready.GITHUB]
    failing = [text for kind, text in counted if kind == ready.FAILURE]
    waiting = [text for kind, text in counted if kind != ready.FAILURE]
    if failing:
        state = FAILURE
    elif waiting:
        state = PENDING
    else:
        return SUCCESS, "Approved at this head; nothing the loop knows holds it."
    lines = ([f"- {text}" for text in failing]
             + ([""] if failing and waiting else [])
             + ([f"- (waiting) {text}" for text in waiting] if failing else
                [f"- {text}" for text in waiting]))
    head = ("Blocked: what has to change, then what is still in progress."
            if state == FAILURE else "In progress: nothing needs a change yet.")
    return state, (head + "\n\n" + "\n".join(lines))[:SUMMARY_MAX]


def title(state: str) -> str:
    """The one-line title GitHub shows next to the check."""
    return {SUCCESS: "Ready to merge", FAILURE: "Not mergeable",
            PENDING: "Waiting"}.get(state, "Waiting")
