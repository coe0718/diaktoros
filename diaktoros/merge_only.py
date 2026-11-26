"""Merge-only heads (#2743): a PR head that only merged its base since the head before it.

When someone merges a PR, the other open PRs are brought up to date: by the host
(``update_behind``, ``review_only_update``), or by a person pressing GitHub's Update branch. The
merge gives each PR a new head with no verdict on it, though nothing of the PR itself changed. A
change request at the old head then went nowhere: the sweep delivered a review that repeated it
(spending a verdict), and the fixer's answer turn queued at the old head was cancelled as
superseded, leaving the PR with no one to move it (#2700, #2738, #2745).

``lookup`` answers, from GitHub alone, whether ``head`` is such a merge: a merge commit whose first
parent is the PR's previous head and whose second is on the loop's base branch, and after which the
PR's own diff against the base is the same file for file (path, status and blob) as before. The
base's new commits then touched none of the PR's files, so a finding at the old head still stands
at the new one. Anything else (a commit that is not a merge, a merge that changed a PR file, a
diff too large to list) is not merge-only, and the head is reviewed as usual. A failed read is
``UNKNOWN``: the claims retry it and the sweep delivers no review on it, instead of deciding.

``carried`` is the change request the old head's latest verdict still makes, as a copy at the new
head marked ``carried_from``; ``gate`` counts it among the head's verdicts, so no review is
delivered there and the fixer's owed answer runs at the new head. An approval is not carried here:
the host's own merges carry it under stricter terms (``base_merge``), and GitHub moves an approval
to the merged head itself.

The reads are commit and compare GETs. What a head's own SHAs fix (its parents, its diff) is kept
per process; whether its merged parent is on the base is read again for every decision, and inside
one (``decision``) every ask gets the answer the first one read.
"""
from __future__ import annotations

import contextlib
import contextvars

from . import gh

# Compare lists at most this many files; a diff that may have been cut is never merge-only.
FILES_MAX = 300
# A head merged from a head that was itself merged: followed this deep at most.
CHAIN_MAX = 8
TOO_LARGE = frozenset({("", "too large", "")})
# Per head, what can't change: its merged parents and whether the PR's diff is the same file for
# file (or "" for a head that is not a two-parent merge, or whose diff changed). Whether the merged
# parent is on the base is read afresh each time: the base branch moves (#2757 review, F2).
_MEMO: dict[tuple[str, str], tuple | str] = {}
_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("merge_only_depth", default=0)


def _read(loop: dict, path: str):
    """One GET as the reader; None when it failed, and the head is reviewed as usual."""
    return gh.api(loop, path, login=loop.get("read_token"))


def _files(loop: dict, base: str, head: str):
    """The PR side's diff from ``base`` to ``head`` as (path, status, blob); ``TOO_LARGE`` when
    GitHub may have cut the list; None unreadable."""
    data = _read(loop, f"/repos/{loop['repo']}/compare/{base}...{head}")
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    files = data["files"]
    if len(files) >= FILES_MAX:
        return TOO_LARGE            # readable, and as large on every retry: never merge-only (F4)
    out = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            return None
        out.add((item["filename"], str(item.get("status") or ""), str(item.get("sha") or "")))
    return frozenset(out)


UNKNOWN = "unknown"      # a read failed: neither merge-only nor not, and never remembered
# Why a guard waits on ``undecided`` (#2757 review, F7, F8).
UNDECIDED_WHY = ("whether a change request carries over the merge to this head is undecided "
                 "(a read failed, or the verdicts before it cannot be ordered)")


def _on_base(loop: dict, commit: str) -> bool | None:
    """Whether ``commit`` is on the loop's base branch (an ancestor of its tip, or the tip), or
    None unreadable. A merge from any other branch is not a base merge (#2757 review, F2)."""
    data = _read(loop, f"/repos/{loop['repo']}/compare/{commit}...{loop['base']}")
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        return None
    return data["status"] in ("identical", "ahead") and data.get("behind_by") == 0


# One decision reads one answer (#2757 review, rounds 3 and 4, F5): the sweep's checks of a PR and
# a claim's reads of a row each ask more than once, and an answer that changed between two asks (a
# read that failed, then recovered) let one check hold and the next proceed. Inside ``decision``
# the first answer for a head stands until the decision ends, UNKNOWN included; it is never kept
# past it, so the next decision reads the base again (F2). Keyed by the loop's base as well as the
# head: an answer for one base is never one for another.
_SCOPE: contextvars.ContextVar[dict | None] = contextvars.ContextVar("merge_only_decision",
                                                                     default=None)


@contextlib.contextmanager
def decision():
    """One decision's reads: every ``lookup`` inside answers as its first one did. A decision
    inside another is part of it."""
    if _SCOPE.get() is not None:
        yield
        return
    token = _SCOPE.set({})
    try:
        yield
    finally:
        _SCOPE.reset(token)


def lookup(loop: dict, head: str) -> str:
    """The PR head ``head`` merged its base into without changing the PR's own diff; ``""`` when
    ``head`` is not such a merge; ``UNKNOWN`` when a read failed (#2757 review, F1). Inside a
    ``decision``, the same answer every time."""
    scope = _SCOPE.get()
    key = (loop["repo"], str(loop.get("base") or ""), head)
    if scope is not None and key in scope:
        return scope[key]
    answer = _decide(loop, head)
    if scope is not None:
        scope[key] = answer
    return answer


def _decide(loop: dict, head: str) -> str:
    """``lookup``'s reads: the immutable facts from the cache, the base afresh."""
    if not isinstance(head, str) or len(head) != 40:
        return ""
    key = (loop["repo"], head)
    facts = _MEMO.get(key)
    if facts is None:
        facts = _facts(loop, head)
        if facts == UNKNOWN:
            return UNKNOWN
        _MEMO[key] = facts
    if not facts:
        return ""
    old, base = facts
    on_base = _on_base(loop, base)
    if on_base is None:
        return UNKNOWN
    return old if on_base else ""


def _facts(loop: dict, head: str):
    """``(old head, merged parent)`` for a two-parent merge that left the PR's diff unchanged,
    ``""`` for any other head, ``UNKNOWN`` when a read failed."""
    commit = _read(loop, f"/repos/{loop['repo']}/commits/{head}")
    if not isinstance(commit, dict) or not isinstance(commit.get("parents"), list):
        return UNKNOWN
    parents = [p.get("sha") if isinstance(p, dict) else None for p in commit["parents"]]
    if len(parents) != 2 or not all(isinstance(p, str) and len(p) == 40 for p in parents):
        return ""
    old, base = parents
    before, after = _files(loop, base, old), _files(loop, base, head)
    if before is None or after is None:
        return UNKNOWN
    same = before is not TOO_LARGE and after is not TOO_LARGE and before and before == after
    return (old, base) if same else ""


def undecided(loop: dict, reviews: list, head: str) -> bool:
    """Whether a change request might be carried to ``head`` but nothing decides it: a read
    failed on the way (#2757 review, F5), or the head it merged from has verdicts none of which is
    provably the latest (an undated or unfamiliar one, F8), so ``carried`` finds none. The sweep
    delivers no review, a queued review waits, and the host's and the conflict merges wait,
    instead of acting on a head whose change request may still be owed."""
    from . import gate
    if not isinstance(reviews, list) or not any(
            isinstance(r, dict) and gh.review_state(r) == "CHANGES_REQUESTED" for r in reviews):
        return False
    current = head
    # The head CHAIN_MAX links back is one ``carried`` reads too, so its verdicts are checked here
    # as well; only the link past it is never followed (#2757 review, round 7).
    for link in range(CHAIN_MAX + 1):
        if current != head and any(gh.review_state(r) not in gate.NOT_VERDICTS
                                   for r in gate.reviews_at_head(reviews, loop, current)):
            # An earlier head with its own verdict decides, when its latest one can be told.
            return gate.latest_effective_review_at_head(reviews, loop, current) is None
        if link == CHAIN_MAX:
            break
        found = lookup(loop, current)
        if found == UNKNOWN:
            return True
        if not found:
            return False
        current = found
    return False


def source(loop: dict, head: str) -> str | None:
    """``lookup``'s merge-only parent, or None: not merge-only, or unknown."""
    found = lookup(loop, head)
    return None if found in ("", UNKNOWN) else found


def leads_to(loop: dict, head: str, old: str) -> bool | None:
    """Whether ``head`` follows ``old`` through merge-only heads alone (at most ``CHAIN_MAX``);
    None when a read on the way failed, so the caller retries rather than decides."""
    current = head
    for _ in range(CHAIN_MAX):
        current = lookup(loop, current)
        if current == UNKNOWN:
            return None
        if not current:
            return False
        if current == old:
            return True
    return False


def carried(loop: dict, reviews: list, head: str) -> list:
    """The change request a merge-only ``head`` still owes an answer to: the previous head's latest
    effective verdict when it requests changes, as a copy at ``head``; ``[]`` otherwise."""
    from . import gate
    depth = _DEPTH.get()
    if depth >= CHAIN_MAX or not isinstance(reviews, list) or not any(
            isinstance(r, dict) and gh.review_state(r) == "CHANGES_REQUESTED" for r in reviews):
        return []                                   # nothing to carry: no read at all
    old = source(loop, head)
    if old is None:
        return []
    token = _DEPTH.set(depth + 1)
    try:
        latest = gate.latest_effective_review_at_head(reviews, loop, old)
    finally:
        _DEPTH.reset(token)
    if latest is None or gh.review_state(latest) != "CHANGES_REQUESTED":
        return []
    return [{**latest, "commit_id": head, "carried_from": old}]
