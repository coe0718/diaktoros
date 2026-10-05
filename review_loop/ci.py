"""The CI state of one commit, as the reader sees it: check runs plus commit statuses.

A reviewer approved a head whose tests had already failed: it was never told the CI result, and
nothing stopped the approval. The reviewer now gets this state as host facts, and the broker
refuses an APPROVE while any check at the head has failed (or the state cannot be read).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import gh

# A check run's conclusions that mean "not green". `neutral` and `skipped` pass, as GitHub's own
# merge box treats them.
FAILED_CONCLUSIONS = {"failure", "timed_out", "action_required", "startup_failure", "cancelled",
                      "stale"}
FAILED_STATUSES = {"failure", "error"}
MAX_PAGES = 3
NAME_MAX = 100
LISTED_MAX = 20


@dataclass
class CIState:
    failed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    @property
    def green(self) -> bool:
        return not self.failed and not self.pending


def _name(raw) -> str:
    return str(raw or "(unnamed)")[:NAME_MAX]


def read(loop: dict, head: str) -> CIState | None:
    """The head's CI state, or None when it cannot be read (a failed or malformed reply).

    Check runs are deduplicated by name, keeping the latest (a re-run replaces a failure).
    """
    repo, reader = loop["repo"], loop.get("read_token")
    latest: dict[str, dict] = {}
    for page in range(1, MAX_PAGES + 1):
        reply = gh.api(loop, f"/repos/{repo}/commits/{head}/check-runs?per_page=100&page={page}",
                       login=reader)
        if not isinstance(reply, dict) or not isinstance(reply.get("check_runs"), list):
            return None
        for run in reply["check_runs"]:
            if not isinstance(run, dict):
                return None
            name = _name(run.get("name"))
            if name not in latest or (run.get("id") or 0) > (latest[name].get("id") or 0):
                latest[name] = run
        total = reply.get("total_count")
        if not isinstance(total, int) or page * 100 >= total:
            break
    else:
        return None   # more check runs than we read: never call a partial view green
    status = gh.api(loop, f"/repos/{repo}/commits/{head}/status", login=reader)
    if not isinstance(status, dict) or not isinstance(status.get("statuses", []), list):
        return None
    state = CIState()
    for name, run in sorted(latest.items()):
        if run.get("status") != "completed":
            state.pending.append(name)
        elif run.get("conclusion") in FAILED_CONCLUSIONS:
            state.failed.append(name)
        else:
            state.passed.append(name)
    for entry in status.get("statuses") or []:
        if not isinstance(entry, dict):
            return None
        name = _name(entry.get("context"))
        value = entry.get("state")
        (state.failed if value in FAILED_STATUSES else
         state.pending if value == "pending" else state.passed).append(name)
    return state


def _names(names: list[str]) -> str:
    shown = ", ".join(json.dumps(name) for name in names[:LISTED_MAX])
    return shown + (f" and {len(names) - LISTED_MAX} more" if len(names) > LISTED_MAX else "")


UNREADABLE = ("the host could not read CI at this head (the reader token may need Checks and "
              "Commit statuses read access)")


def approval_refusal(state: CIState | None) -> str:
    """Why an APPROVE at this head is refused, or "" when CI does not stand in its way."""
    if state is None:
        return (f"APPROVE refused: {UNREADABLE}; nothing was written. Submit REQUEST_CHANGES "
                "or ask for the review to be retried.")
    if state.failed:
        return (f"APPROVE refused: CI has failed at this head ({_names(state.failed)}); nothing "
                "was written. Submit REQUEST_CHANGES naming the failed checks.")
    return ""


def section(state: CIState | None) -> str:
    """The reviewer's CI facts: names are data from GitHub, JSON-quoted."""
    head = "\n\n## CI at this head (read by the host from GitHub just before this turn; data)\n\n"
    if state is None:
        return head + f"Unknown: {UNREADABLE}. An APPROVE will be refused until it can be read."
    if not (state.failed or state.pending or state.passed):
        return head + "No checks or statuses are reported for this commit."
    lines = []
    if state.failed:
        lines.append(f"- **failed:** {_names(state.failed)}")
    if state.pending:
        lines.append(f"- **still running:** {_names(state.pending)}")
    if state.passed:
        lines.append(f"- passed: {len(state.passed)}")
    return head + "\n".join(lines)
