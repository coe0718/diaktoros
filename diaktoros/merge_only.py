"""Merge-only heads (#2743): a PR head that only merged its base since the head before it.

When someone merges a PR, the other open PRs are brought up to date: by the host
(``update_behind``, ``review_only_update``), or by a person pressing GitHub's Update branch. The
merge gives each PR a new head with no verdict on it, though nothing of the PR itself changed. A
change request at the old head then went nowhere: the sweep delivered a review that repeated it
(spending a verdict), and the fixer's answer turn queued at the old head was cancelled as
superseded, leaving the PR with no one to move it (#2700, #2738, #2745).

``lookup`` answers, from GitHub alone, whether ``head`` is such a merge: a merge commit whose first
parent is the PR's previous head and whose second is on the loop's base branch, and after which the
PR's own diff against the base is the same file for file (path, status and blob) as before. The base's new commits then touched none of the
PR's files, so a finding at the old head still stands at the new one. Anything else (a commit
that is not a merge, a merge that changed a PR file, a diff too large to list, a read that failed)
is not merge-only, and the head is reviewed as usual; a failed read is ``UNKNOWN``, which the fixer's
claim retries instead of deciding.

``carried`` is the change request the old head's latest verdict still makes, as a copy at the new
head marked ``carried_from``; ``gate`` counts it among the head's verdicts, so no review is
delivered there and the fixer's owed answer runs at the new head. An approval is not carried here:
the host's own merges carry it under stricter terms (``base_merge``), and GitHub moves an approval
to the merged head itself.

The reads are commit and compare GETs on immutable SHAs, so GitHub answers repeats from its
cache; answers are also kept per process.
"""
from __future__ import annotations

import contextvars

from . import gh

# Compare lists at most this many files; a diff that may have been cut is never merge-only.
FILES_MAX = 300
# A head merged from a head that was itself merged: followed this deep at most.
CHAIN_MAX = 8
_MEMO: dict[tuple[str, str], str] = {}
_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("merge_only_depth", default=0)


def _read(loop: dict, path: str):
    """One GET as the reader; None when it failed, and the head is reviewed as usual."""
    return gh.api(loop, path, login=loop.get("read_token"))


def _files(loop: dict, base: str, head: str) -> frozenset | None:
    """The PR side's diff from ``base`` to ``head`` as (path, status, blob), or None unreadable."""
    data = _read(loop, f"/repos/{loop['repo']}/compare/{base}...{head}")
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    files = data["files"]
    if len(files) >= FILES_MAX:
        return None
    out = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            return None
        out.add((item["filename"], str(item.get("status") or ""), str(item.get("sha") or "")))
    return frozenset(out)


UNKNOWN = "unknown"      # a read failed: neither merge-only nor not, and never remembered


def _on_base(loop: dict, commit: str) -> bool | None:
    """Whether ``commit`` is on the loop's base branch (an ancestor of its tip, or the tip), or
    None unreadable. A merge from any other branch is not a base merge (#2757 review, F2)."""
    data = _read(loop, f"/repos/{loop['repo']}/compare/{commit}...{loop['base']}")
    if not isinstance(data, dict) or not isinstance(data.get("status"), str):
        return None
    return data["status"] in ("identical", "ahead") and data.get("behind_by") == 0


def lookup(loop: dict, head: str) -> str:
    """The PR head ``head`` merged its base into without changing the PR's own diff; ``""`` when
    ``head`` is not such a merge; ``UNKNOWN`` when a read failed (#2757 review, F1)."""
    if not isinstance(head, str) or len(head) != 40:
        return ""
    key = (loop["repo"], head)
    if key in _MEMO:
        return _MEMO[key]
    commit = _read(loop, f"/repos/{loop['repo']}/commits/{head}")
    if not isinstance(commit, dict) or not isinstance(commit.get("parents"), list):
        return UNKNOWN
    parents = [p.get("sha") if isinstance(p, dict) else None for p in commit["parents"]]
    if len(parents) != 2 or not all(isinstance(p, str) and len(p) == 40 for p in parents):
        _MEMO[key] = ""
        return ""
    old, base = parents
    on_base = _on_base(loop, base)
    if on_base is None:
        return UNKNOWN
    if not on_base:
        return ""                  # not remembered: the branch may reach the base later
    before, after = _files(loop, base, old), _files(loop, base, head)
    if before is None or after is None:
        return UNKNOWN
    _MEMO[key] = old if before and before == after else ""
    return _MEMO[key]


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
