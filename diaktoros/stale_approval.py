"""#473: an approval says "good at this head". If a required check at that same head then goes
red, is cancelled, or never reports within its wait, the approval is stale.

The watchdog sweep records the finding in its ``watch`` state (so ``explain`` can show it and the
ready-to-merge queue can exclude the PR through :func:`is_stale`) and sends one ``stale_approval``
notice per PR and head. Nothing is written to GitHub. A PR that is not approved at its current head
is never flagged, and a new head clears the record (it has no approval yet).

A missing required check only counts once ``MISSING_WAIT_S`` has passed since the approval was
submitted. It can only fire when ``required_checks`` names checks: with none, every reported check
gates and none can be "missing".
"""
from __future__ import annotations

import time

from . import ci, config, gh

KEY = "stale_approvals"
MISSING_WAIT_S = 30 * 60


def _epoch(review: dict) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(review["submitted_at"].replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def assess(loop: dict, state: ci.CIState | None, approved_at: float | None,
           now: float) -> str:
    """Why an approval at a head is stale given its CI, or "" when it stands.

    Unreadable CI is not a finding. Pending is not a finding.
    """
    gate = ci.gating(state, config.required_checks(loop))
    if gate is None:
        return ""
    if gate.failed:
        return f"{ci._names(gate.failed)} is red"
    if gate.cancelled:
        return f"{ci._names(gate.cancelled)} was cancelled"
    if gate.missing and approved_at is not None and now - approved_at >= MISSING_WAIT_S:
        return f"{ci._names(gate.missing)} never reported"
    return ""


def record(watch: dict, number: int) -> dict | None:
    entry = (watch.get(KEY) or {}).get(str(number)) if isinstance(watch.get(KEY), dict) else None
    return entry if isinstance(entry, dict) else None


def is_stale(watch: dict, number: int, head: str) -> bool:
    """Whether the approval at ``head`` was found stale (for the ready-to-merge queue, #479)."""
    entry = record(watch, number)
    return bool(entry and head and entry.get("head") == head)


def line(entry: dict) -> str:
    return f"approval at {str(entry.get('head') or '?')[:7]} is stale: {entry.get('why')}"


def sweep(loop: dict, st, prs: list, watch: dict, log=lambda _msg: None) -> None:
    """Flag approved loop PRs whose required CI went bad at the approved head. No model, no write."""
    from . import gate, observer, transition
    kept: dict[str, dict] = {}
    previous = watch.get(KEY) if isinstance(watch.get(KEY), dict) else {}
    now = time.time()
    for pr in prs:
        if not isinstance(pr, dict) or pr.get("draft"):
            continue
        author = ((pr.get("user") or {}).get("login") or "").lower()
        number, head = pr.get("number"), (pr.get("head") or {}).get("sha") or ""
        if (author not in config.reviewed_authors(loop) or not number or not head
                or (pr.get("base") or {}).get("ref") != loop["base"]):
            continue
        old = previous.get(str(number))
        errors: list[str] = []
        reviews = transition.effective_reviews(loop, st, number, head,
                                               gh.reviews(loop, number, errors=errors))
        if not isinstance(reviews, list):
            if isinstance(old, dict):
                kept[str(number)] = old       # unknown is not cleared
            continue
        latest = gate.latest_effective_review_at_head(reviews, loop, head)
        if latest is None or gh.review_state(latest) != "APPROVED":
            continue                          # not approved at this head: never flagged
        state = ci.read(loop, head)
        if state is None:
            if isinstance(old, dict) and old.get("head") == head:
                kept[str(number)] = old       # unreadable CI neither flags nor clears
            continue
        why = assess(loop, state, _epoch(latest), now)
        if not why:
            continue                          # green (or still running): cleared
        entry = {"head": head, "why": why, "at": (old or {}).get("at")
                 if isinstance(old, dict) and old.get("head") == head else now}
        kept[str(number)] = entry
        observer.notify(loop, st, "stale_approval", number, head, identity="stale_approval",
                        outcome=line(entry)[:400],
                        next_turn="you: the approval no longer vouches for this head — re-run "
                                  "the check or push a fix; the ready-to-merge queue skips it")
        log(f"#{number} @ {head[:7]} {line(entry)}")
    watch[KEY] = kept
