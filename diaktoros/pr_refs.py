"""The PR a commit names, read from its subject alone (#1635, #1699).

A squash or rebase merge names its PR as ``title (#N)``; a merge commit as ``Merge pull request #N``.
Nothing else is asked of GitHub, so a direct push whose subject ends in ``(#N)`` (an issue number,
say) is counted as PR #N too. Every reader that names merged PRs from commits uses this module —
``hermes dk update``, the ``main_red`` notice and the review-only conflict notice — and the two
notices carry ``CAVEAT`` so they do not state a subject-only reading as a fact.
"""
from __future__ import annotations

import re

DIGITS_MAX = 10        # GitHub numbers are 32-bit; a longer (#N) is no PR (review of #2419)
SQUASH = re.compile(rf"\s*\(#(\d{{1,{DIGITS_MAX}}})\)\s*$")
MERGE = re.compile(rf"^Merge pull request #(\d{{1,{DIGITS_MAX}}})\b")
CAVEAT = "read from commit subjects, so a direct push ending in (#N) is counted as a PR too"


def pr_number(subject: str) -> int:
    """The PR number a commit subject names (merge commit first, then a trailing ``(#N)``), or 0.

    A reference longer than ``DIGITS_MAX`` digits names no PR: it keeps a pushed subject from
    growing the notices past their bound (or ``int()`` past its 4300-digit limit)."""
    subject = subject or ""
    match = MERGE.match(subject) or SQUASH.search(subject)
    return int(match.group(1)) if match else 0
