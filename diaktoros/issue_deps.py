"""Issue dependencies a maintainer declares (#671): issue B's fix waits for issue A's
implementation to be merged into the loop's base.

Declared only by the operator (``hermes dk depends``), never inferred from issue text (#686,
#708). A prerequisite counts as done only on evidence that its implementation is in the base:

- an issue-fix PR the ledger recorded for it, merged into the base; or
- the issue closed by a commit GitHub says the base contains (a ``Fixes #A`` merge).

Closing A any other way is not done: B keeps waiting until a maintainer waives the dependency.
An unreadable fact is never guessed: B waits, and the reason says GitHub could not be read.
"""
from __future__ import annotations

from . import gh

EVENT_PAGES = 3


def problem(deps: dict, issue: int, prerequisite: int) -> str:
    """Why ``issue`` may not depend on ``prerequisite`` given the declared ``deps``, or ""."""
    if issue == prerequisite:
        return f"#{issue} cannot depend on itself"
    # A cycle: the prerequisite already (transitively) depends on the issue.
    seen, stack, parent = set(), [prerequisite], {prerequisite: None}
    while stack:
        node = stack.pop()
        if node == issue:
            path, step = [], node
            while step is not None:
                path.append(f"#{step}")
                step = parent[step]
            return "a cycle: " + " → ".join([f"#{issue}", *reversed(path)])
        if node in seen:
            continue
        seen.add(node)
        for nxt in (deps.get(node) or {}):
            if nxt not in parent:
                parent[nxt] = node
            stack.append(nxt)
    return ""


def read_issue(loop: dict, number: int) -> dict | None:
    """The issue as the reader sees it; None when unreadable."""
    data = gh.api(loop, f"/repos/{loop['repo']}/issues/{number}", login=loop["read_token"])
    return data if isinstance(data, dict) and data.get("number") == number else None


def _in_base(loop: dict, sha: str) -> bool | None:
    """Does the base branch contain ``sha``? None when GitHub cannot say."""
    data = gh.api(loop, f"/repos/{loop['repo']}/compare/{sha}...{loop['base']}",
                  login=loop["read_token"])
    if not isinstance(data, dict) or not data.get("status"):
        return None
    return data["status"] in ("ahead", "identical")


def _fix_prs(loop: dict, number: int) -> list[int]:
    """The issue-fix PRs the ledger recorded for ``number``, newest first."""
    from .run_supervisor import Supervisor, production_ledger
    db = production_ledger()
    if not db.exists():
        return []
    return Supervisor(db).issue_fix_prs(loop["repo"], number)


def met(loop: dict, prerequisite: int) -> tuple[bool | None, str]:
    """Is ``prerequisite``'s implementation merged into the base? ``(True, evidence)``,
    ``(False, why not yet)`` or ``(None, what could not be read)``."""
    base = loop["base"]
    for pr in _fix_prs(loop, prerequisite):
        data = gh.api(loop, gh.pr_path(loop, pr), login=loop["read_token"])
        if not isinstance(data, dict) or data.get("number") != pr:
            return None, f"#{prerequisite}'s fix PR #{pr} could not be read"
        if data.get("merged_at") and (data.get("base") or {}).get("ref") == base:
            return True, f"#{prerequisite}'s fix PR #{pr} merged into {base}"
    issue = read_issue(loop, prerequisite)
    if issue is None:
        return None, f"#{prerequisite} could not be read"
    if issue.get("state") == "open":
        return False, f"#{prerequisite} is still open"
    events, why = gh._read_pages(
        loop, f"/repos/{loop['repo']}/issues/{prerequisite}/events?per_page=100",
        "event", EVENT_PAGES, login=loop["read_token"])
    if events is None:                    # unreadable or past the bound: never a prefix (#765)
        return None, f"#{prerequisite}'s events could not be read in full ({why})"
    closer = ""
    for event in events:
        if event.get("event") == "closed":
            closer = str(event.get("commit_id") or "")         # the last close wins
    if closer:
        inside = _in_base(loop, closer)
        if inside is None:
            return None, f"whether {base} contains {closer[:7]} could not be read"
        if inside:
            return True, f"#{prerequisite} was closed by {closer[:7]}, which {base} contains"
    return False, (f"#{prerequisite} was closed without an implementation merged into {base} — "
                   f"waive it with `hermes dk depends --loop {loop['id']} --issue <dependent> "
                   f"--waive {prerequisite}` if it should not block")


def blocking(loop: dict, st, issue: int) -> list[tuple[int, str]]:
    """The prerequisites of ``issue`` not yet done (or unreadable), with why: ``[]`` lets it go."""
    out = []
    for prerequisite in sorted((st.issue_deps().get(issue) or {})):
        try:
            done, why = met(loop, prerequisite)
        except Exception as exc:                      # never guessed: an error blocks
            done, why = None, f"#{prerequisite}: {type(exc).__name__}: {exc}"
        if done is not True:
            out.append((prerequisite, why))
    return out


def wait(loop: dict, st, issue: int, blockers: list[tuple[int, str]]) -> None:
    """Record that ``issue``'s hand-off waits, and say so once on the issue."""
    from . import config
    st.dep_wait_set(issue)
    if st.dep_wait_said(issue):
        return
    login = config.triage_login(loop)
    if login:
        names = ", ".join(f"#{a}" for a, _ in blockers)
        reasons = "; ".join(why for _, why in blockers)
        posted = gh.api(loop, f"/repos/{loop['repo']}/issues/{issue}/comments", method="POST",
                        body={"body": f"waiting for {names} — a maintainer made this issue "
                                      f"depend on {'it' if len(blockers) == 1 else 'them'}, and "
                                      f"the implementation isn't merged into {loop['base']} yet "
                                      f"({reasons})."}, login=login)
        if posted is None:
            return                                    # not said: a later sweep retries
    st.dep_wait_say(issue)
