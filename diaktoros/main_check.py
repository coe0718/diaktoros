"""Verify main after merges: one notice when a required check is red on main's head.

Two PRs can each be green and approved, yet main is red once both are merged. The watchdog reads
main's checks at its current head (no model, no GitHub write). A required check red there is one
``main_red`` observer notice per head, naming the check and the PRs merged since main's last
green head (read from commit subjects, which the notice says: ``pr_refs``, #1699). A green main
sends nothing and becomes the new last green head.

With ``main_flake_rerun`` on, a red that looks like a flake has its failed jobs rerun once first,
as a maintainer, and the notice waits for that rerun (``main_flake``, #1784): the one write here.

GitHub's "Require branches to be up to date before merging" branch protection prevents this class
of breakage; the notice says so.
"""
from __future__ import annotations

from . import ci, config, gh, pr_refs

FILE = "main-ci.json"
PRS_MAX = 10
ADVICE = ("GitHub's \"Require branches to be up to date before merging\" protection prevents "
          "this class of breakage")
OUTCOME_MAX = 600       # the main_red notice's outcome is at most this long
NAMES_MIN = 40          # what the failed check names keep at least, when they are clipped


class Merges(list):
    """PR numbers read from one compare; ``seen``/``total`` are the commits read vs. reported.

    ``partial`` is true unless GitHub's ``total_commits`` is present and equals the commits
    returned: GitHub truncates the ``commits`` array (250) and reports the true count there.
    """

    def __init__(self, numbers=(), seen: int = 0, total=None):
        super().__init__(numbers)
        self.seen, self.total = seen, total

    @property
    def reported(self) -> bool:
        """True when GitHub gave a usable ``total_commits`` (an int, not a bool)."""
        return isinstance(self.total, int) and not isinstance(self.total, bool)

    @property
    def partial(self) -> bool:
        return not (self.reported and self.total == self.seen)


def merged_since(loop: dict, green: str, head: str) -> Merges | None:
    """PR numbers merged after ``green`` up to ``head`` (oldest first); None when unreadable.

    The result's ``partial`` says the compare was truncated (or its count unreadable)."""
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
        number = pr_refs.pr_number(message.splitlines()[0])
        if number and number not in out:
            out.append(number)
    return Merges(out, len(commits), data.get("total_commits"))


def _outcome(failed: list[str], merges: list[int] | None, url: str, note: str = "") -> str:
    if merges is None:
        since = "merged PRs unknown (no earlier green main head recorded or readable)"
    else:
        partial = bool(getattr(merges, "partial", False))
        if not merges:
            since = ("no PR merges found in the part of the compare GitHub returned" if partial
                     else "no PR merges since main's last green head")
        else:
            shown = ", ".join(f"#{n}" for n in merges[-PRS_MAX:])
            more = f" (+{len(merges) - PRS_MAX} earlier)" if len(merges) > PRS_MAX else ""
            since = ("merged PRs seen so far (partial list): " if partial
                     else "merged since main's last green head: ") + f"{shown}{more}"
            since += f" ({pr_refs.CAVEAT})"
        if partial:
            seen = merges.seen
            if getattr(merges, "reported", False):
                said = f"GitHub's compare was truncated ({seen} of {merges.total} commits read)"
            else:                          # count absent/unreadable: say only what was observed
                said = (f"could not confirm GitHub's compare was complete "
                        f"({seen} commit{'' if seen == 1 else 's'} read, total unreported)")
            since += f" — {said}, so other merged PRs may be missing; this is not the full list"
    return _bounded(ci._names(failed), f" — {since}",
                    f". {ADVICE}{note}" + (f" — {url}" if url else ""))


def _bounded(names: str, since: str, rest: str) -> str:
    """``main: required check(s) failed: <names><since><rest>`` in at most ``OUTCOME_MAX``
    characters, with ``since`` (the merged-PR list and its subject-only caveat, #1699) kept whole.

    The failed check names are clipped first (to no less than ``NAMES_MIN``), then ``rest`` (the
    advice, the flake note, the link) is cut at the end: the list is never cut off mid-caveat."""
    lead = "main: required check(s) failed: "
    room = max(OUTCOME_MAX - len(lead) - len(since) - len(rest), NAMES_MIN)
    if len(names) > room:
        names = names[:room - 1] + "…"
    head = f"{lead}{names}{since}"
    return head + rest[:max(OUTCOME_MAX - len(head), 0)]


def _remember(st, path, key: str, head) -> None:
    """Record ``key`` = head (or a rerun's record), re-reading the file under the lock so the
    other keys survive."""
    with st.locked():
        current = st.kv_get(path, {})
        current = current if isinstance(current, dict) else {}
        st.kv_set(path, {**current, key: head})


def _green(loop: dict, st, path, memory: dict, head: str) -> None:
    """Record ``head`` green; a head turned green by the flake rerun gets its one routine
    ``main_flaked`` notice (#1784)."""
    from . import main_flake, observer
    _remember(st, path, "green", head)
    tried = memory.get("rerun")
    if isinstance(tried, dict) and tried.get("head") == head:
        observer.notify(loop, st, "main_flaked", 0, head, identity="main_flaked",
                        outcome=main_flake.flaked_outcome(tried)[:600], next_turn="")


def sweep(loop: dict, st, log=lambda _msg: None) -> bool:
    """One watchdog pass over main (no model, no write). True when a notice was sent now."""
    from . import observer
    ref = gh.api(loop, f"/repos/{loop['repo']}/commits/{loop['base']}",
                 login=loop.get("read_token"))
    head = ref.get("sha") if isinstance(ref, dict) else None
    if not isinstance(head, str) or not head:
        return False                                  # unreadable main is no notice
    path = st.dir / FILE
    memory = st.kv_get(path, {})
    memory = memory if isinstance(memory, dict) else {}
    if head == memory.get("green"):
        return False                                  # this head is already judged green
    view = ci.gating(ci.read(loop, head), config.required_checks(loop))
    if view is None:
        return False                                  # unreadable CI is no notice
    if head == memory.get("notified"):
        # Said red once; a re-run that turns it green is recorded (#681), so the next red
        # names the merges since this head, not since an older green one.
        if view.green:
            _remember(st, path, "green", head)
        return False
    if not view.failed:
        if view.green:
            _green(loop, st, path, memory, head)
        return False                                  # green or still running: nothing yet
    from . import main_flake
    decided = main_flake.decide(loop, memory, view, head)   # #1784: a flake is rerun first
    if decided.action == "rerun":
        _remember(st, path, "rerun", decided.record)
        log(f"main @ {head[:7]} looks flaky ({decided.record['why']}): reran runs "
            f"{decided.record['runs']}")
    if decided.action != "notice":
        return False
    merges = merged_since(loop, memory.get("green") or "", head)
    outcome = _outcome(view.failed, merges, view.urls.get(view.failed[0], ""), decided.note)
    _remember(st, path, "notified", head)             # remember first: one notice per head
    observer.notify(loop, st, "main_red", merges[-1] if merges else 0, head,
                    identity="main_red", outcome=outcome, next_turn="you")
    log(f"main @ {head[:7]} is red: {ci._names(view.failed)}")
    return True
