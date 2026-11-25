"""``selftest``'s step 5 hints (split from ``selftest`` at its size limit): the fix for each denial
``broker.authorize`` can raise for the reviewer write the selftest dry-runs (#1497, #2067).

A key is the denial's exact wording, with the login a token denial names as ``{identity}``.
``tests/test_selftest_denials.py`` holds the keys to what the broker raises and fails on a
reviewer-reachable denial with no key here.
"""
from __future__ import annotations

import re

GENERIC = "fix the reason above; the broker checks it before every write"
_IDENTITIES = "see the identities step"

DENIAL_FIX = {
    "PR identity, state or draft status changed": "the PR must be open and not a draft",
    "PR base branch mismatch": "the PR must target the loop's base branch",
    "PR base repository mismatch": "the PR must be in the loop's repository",
    "fork head not permitted for credentialed writes": "use a PR whose head branch is in the repository itself, not a fork",
    "cannot verify live PR": "check the PR number and that the read token can see it",
    # The broker re-reads the PR: these are its head as that read found it.
    "stale PR head": "the PR's head moved after the selftest read it; run the selftest again",
    "wrong destination branch": ("the PR's head branch changed after the selftest read it, or its name is "
                                 "empty or starts with '-'; run the selftest again"),
    # The seat and read mapping check, then broker._verify_identities' wording with the names
    # ``authorize`` passes it: each is a fact step 4 checks per identity.
    "explicit seat and read credential mappings required": _IDENTITIES,
    "read, reviewer and fixer must use distinct identities": _IDENTITIES,
    "read, reviewer and fixer credential mappings required": _IDENTITIES,
    "read, reviewer and fixer must use distinct credential files": _IDENTITIES,
    "empty token for {identity}": _IDENTITIES,
    "missing token for {identity}": _IDENTITIES,
    "token principal cannot be verified": _IDENTITIES,
    "read, reviewer and fixer tokens resolve to the same principal": _IDENTITIES,
}


def fix_for(denial: str) -> str:
    """The hint for ``denial``, a ``BrokerDenied`` message; ``GENERIC`` for one with no key."""
    return DENIAL_FIX.get(re.sub(r"(token for) \S+\Z", r"\1 {identity}", denial), GENERIC)
