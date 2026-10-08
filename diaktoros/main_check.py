"""Verify main after merges: one notice when a required check is red on main's head.

Two PRs can each be green and approved, yet main is red once both are merged. The watchdog reads
main's checks at its current head (no model, no GitHub write). A required check red there is one
``main_red`` observer notice per head, naming the check and the PRs merged since main's last
green head. A green main sends nothing and becomes the new last green head.

GitHub's "Require branches to be up to date before merging" branch protection prevents this class
of breakage; the notice says so.
"""
from __future__ import annotations

import re

from . import ci, config, gh

FILE = "main-ci.json"
PRS_MAX = 10
ADVICE = ("GitHub's \"Require branches to be up to date before merging\" protection prevents "
          "this class of breakage")
_PR_REF = re.compile(r"\(#(\d+)\)\s*$|^Merge pull request #(\d+)")


def merged_since(loop: dict, green: str, head: str) -> list[int] | None:
    """PR numbers merged after ``green`` up to ``head`` (oldest first); None when unreadable."""
    if not green:
        return None
    data = gh.api(loop, f"/repos/{loop['repo']}/compare/{green}...{head}",
                  login=loop.get("read_token"))
    commits = data.get("commits") if isinstance(data, dict) else None
    if not isinstance(commits, list):
        return None
    out: list[int] = []
    for commit in commits:
        message = ((commit.get("commit") or {}).get("message") if isinstance(commit, dict) else None)
        if not isinstance(message, str) or not message:
            continue
        match = _PR_REF.search(message.splitlines()[0])
        number = int(match.group(1) or match.group(2)) if match else 0
        if number and number not in out:
            out.append(number)
    return out


def _outcome(failed: list[str], merges: list[int] | None, url: str) -> str:
    if merges is None:
        since = "merged PRs unknown (no earlier green main head recorded or readable)"
    elif not merges:
        since = "no PR merges since main's last green head"
    else:
        shown = ", ".join(f"#{n}" for n in merges[-PRS_MAX:])
        more = f" (+{len(merges) - PRS_MAX} earlier)" if len(merges) > PRS_MAX else ""
        since = f"merged since main's last green head: {shown}{more}"
    text = f"main: required check(s) failed: {ci._names(failed)} — {since}. {ADVICE}"
    return f"{text} — {url}" if url else text


def sweep(loop: dict, st, log=lambda _msg: None) -> bool:
    """One watchdog pass over main (no model, no write). True when a notice was sent now."""
    from . import observer
    ref = gh.api(loop, f"/repos/{loop['repo']}/commits/{loop['base']}",
                 login=loop.get("read_token"))
    head = ref.get("sha") if isinstance(ref, dict) else None
    if not isinstance(head, str) or not head:
        return False                                  # unreadable main is no notice
    path = st.dir / FILE
    memory = st._load(path, {})
    memory = memory if isinstance(memory, dict) else {}
    if head in (memory.get("notified"), memory.get("green")):
        return False                                  # this head is already judged
    view = ci.gating(ci.read(loop, head), config.required_checks(loop))
    if view is None:
        return False                                  # unreadable CI is no notice
    if not view.failed:
        if view.green:
            with st.locked():
                st._save(path, {**memory, "green": head})
        return False                                  # green or still running: nothing yet
    merges = merged_since(loop, memory.get("green") or "", head)
    outcome = _outcome(view.failed, merges, view.urls.get(view.failed[0], ""))
    with st.locked():                                 # remember first: one notice per head
        st._save(path, {**memory, "notified": head})
    observer.notify(loop, st, "main_red", merges[-1] if merges else 0, head,
                    identity="main_red", outcome=outcome[:600], next_turn="you")
    log(f"main @ {head[:7]} is red: {ci._names(view.failed)}")
    return True
