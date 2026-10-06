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
FAILED_CONCLUSIONS = {"failure", "timed_out", "action_required", "startup_failure"}
# Not green, but not the change's fault either (#363): GitHub cancelled the run (a superseded run,
# or no hosted runner acquired) or marked it stale. It needs a re-run, not a fix.
CANCELLED_CONCLUSIONS = {"cancelled", "stale"}
FAILED_STATUSES = {"failure", "error"}
MAX_PAGES = 3
NAME_MAX = 100
LISTED_MAX = 20


@dataclass
class CIState:
    failed: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # required, never reported at this head
    ids: dict = field(default_factory=dict)    # check run id by name (an Actions job id) (#306)
    urls: dict = field(default_factory=dict)   # where GitHub shows each run (#306)

    @property
    def green(self) -> bool:
        return not (self.failed or self.pending or self.cancelled or self.missing)


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
        if type(run.get("id")) is int:
            state.ids[name] = run["id"]
        if isinstance(run.get("html_url"), str):
            state.urls[name] = run["html_url"][:300]
        if run.get("status") != "completed":
            state.pending.append(name)
        elif run.get("conclusion") in FAILED_CONCLUSIONS:
            state.failed.append(name)
        elif run.get("conclusion") in CANCELLED_CONCLUSIONS:
            state.cancelled.append(name)
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


def gating(state: CIState | None, required) -> CIState | None:
    """The checks that gate (#368): all of them when the loop names none; otherwise only the
    required ones. A required check that never reported at this head is `missing`, not running:
    it refuses an approval and holds a review whatever review_after_ci says."""
    if state is None or not required:
        return state
    wanted = list(dict.fromkeys(required))
    keep = lambda names: [name for name in names if name in wanted]  # noqa: E731
    view = CIState(failed=keep(state.failed), pending=keep(state.pending),
                   passed=keep(state.passed), cancelled=keep(state.cancelled),
                   ids=state.ids, urls=state.urls)
    reported = set(state.failed) | set(state.pending) | set(state.passed) | set(state.cancelled)
    view.missing = [name for name in wanted if name not in reported]
    return view


def optional(state: CIState | None, required) -> CIState | None:
    """The checks that do not gate (#368); None when every check gates."""
    if state is None or not required:
        return None
    drop = lambda names: [name for name in names if name not in required]  # noqa: E731
    return CIState(failed=drop(state.failed), pending=drop(state.pending),
                   passed=drop(state.passed), cancelled=drop(state.cancelled))


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
    if state.cancelled:
        return (f"APPROVE refused: CI at this head was cancelled ({_names(state.cancelled)}) and "
                "needs a re-run; nothing was written. That is not a defect of the change: do not "
                "list it as a finding. Submit REQUEST_CHANGES only for real findings; otherwise "
                "say in your summary that you would approve once CI is re-run and passes.")
    if state.missing:
        return (f"APPROVE refused: required check(s) never reported at this head "
                f"({_names(state.missing)}); nothing was written. A required check that does not "
                "appear would gate nothing, so it may be misnamed or dropped from the workflow. "
                "That is for the operator to fix, not a finding on the change: say so in your "
                "summary and do not approve.")
    return ""


def section(state: CIState | None, required=()) -> str:
    """The reviewer's CI facts: names are data from GitHub, JSON-quoted. With required checks
    (#368), the gating ones first, then the rest marked optional."""
    head = "\n\n## CI at this head (read by the host from GitHub just before this turn; data)\n\n"
    if state is not None and required:
        extra = optional(state, required)
        lines = _lines(gating(state, required))
        text = head + "Required checks (these gate the approval):\n" + ("\n".join(lines) or "- none")
        rest = [f"- {label}: {_names(names)}" for label, names in
                (("failed", extra.failed), ("cancelled", extra.cancelled),
                 ("still running", extra.pending)) if names]
        if extra.passed:
            rest.append(f"- passed: {len(extra.passed)}")
        if rest:
            text += ("\n\nOptional checks (shown for context; they never block, and are not "
                     "findings on their own):\n" + "\n".join(rest))
        return text
    if state is None:
        return head + f"Unknown: {UNREADABLE}. An APPROVE will be refused until it can be read."
    if not (state.failed or state.pending or state.passed or state.cancelled):
        return head + "No checks or statuses are reported for this commit."
    return head + "\n".join(_lines(state))


def _lines(state: CIState) -> list[str]:
    lines = []
    if state.failed:
        lines.append(f"- **failed:** {_names(state.failed)}")
    if state.pending:
        lines.append(f"- **still running:** {_names(state.pending)}")
    if state.missing:
        lines.append(f"- **not reported at this head yet:** {_names(state.missing)}")
    if state.cancelled:
        lines.append(f"- **cancelled** (needs a re-run; not a defect of the change): "
                     f"{_names(state.cancelled)}")
    if state.passed:
        lines.append(f"- passed: {len(state.passed)}")
    return lines
