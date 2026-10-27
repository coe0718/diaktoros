"""Learn from overrides (#220): record when a human disagrees with the loop.

First case: a PR merged over a standing changes-requested. The host reads the repository's
recently merged PRs and their reviews; when the loop reviewer's latest live verdict at the merged
head is CHANGES_REQUESTED, one record goes to the run ledger (``overrides``): PR, head, seat, the
model of the reviewer run that posted the verdict, and the numbered findings still open then, each
with its category (``findings.CATEGORIES``). The weekly report lists them, with a count per
category. Nothing here changes behaviour. Dismissed reviews, reversed rulings and
reverted fixer pushes are not detected yet.

A merge after an approval writes nothing, and neither does one whose reviews cannot be read or
ordered: only a verdict proved current counts. Such a merge is read again on each sweep while it is
inside the 7-day window (``LOOKBACK_S``); one still unreadable or unordered then is never recorded.
"""
from __future__ import annotations

import time

from . import config, findings, gate, gh

MERGED_OVER_CHANGES = "merged_over_changes"
LOOKBACK_S = 7 * 86400      # a merge older than the report's window is not looked at
LISTED = 30                 # recently updated closed PRs read per sweep (one request)


def scan(loop: dict, st, sup, now: float | None = None) -> list[int]:
    """Record the merges over a changes-requested not yet recorded; the PR numbers recorded."""
    from . import stats
    now = time.time() if now is None else now
    reader = loop.get("read_token")
    listing = gh.api(loop, f"/repos/{loop['repo']}/pulls?state=closed&sort=updated"
                           f"&direction=desc&per_page={LISTED}", login=reader)
    if not isinstance(listing, list):
        return []
    served = {a.lower() for a in config.reviewed_authors(loop)}
    checked, kept, recorded = st.override_checked(), {}, []
    for pr in listing:
        if not isinstance(pr, dict) or not pr.get("merged_at"):
            continue
        number, head = pr.get("number"), (pr.get("head") or {}).get("sha")
        merged_at = stats._iso(pr["merged_at"])
        author = str((pr.get("user") or {}).get("login") or "").lower()
        if (not isinstance(number, int) or not head or merged_at is None
                or merged_at < now - LOOKBACK_S or author not in served):
            continue
        if checked.get(str(number)) == head:
            kept[str(number)] = head
            continue
        reviews, _error = gh.reviews_read(loop, number, login=reader)
        if reviews is None:
            continue                       # unknown, not "approved": read again next sweep
        latest = gate.latest_effective_review_at_head(reviews, loop, head)
        if latest is None and any(gh.review_state(r) not in gate.NOT_VERDICTS
                                  for r in gate.reviews_at_head(reviews, loop, head)):
            continue                       # a verdict whose order is unproved: read again
        kept[str(number)] = head
        if latest is None or gh.review_state(latest) != "CHANGES_REQUESTED":
            continue
        review_id = int(latest["id"])
        entry = st.findings_get(number)
        open_found = [{"id": fid, "category": str((entry["findings"][fid] or {}).get("category")
                                                   or "other")}
                      for fid in findings.open_ids(entry)]
        if sup.record_override(loop["repo"], number, head, MERGED_OVER_CHANGES, review_id,
                               sup.approving_model(loop["repo"], number, head, review_id),
                               open_found, merged_at):
            recorded.append(number)
    st.override_checked_put(kept)
    return recorded
