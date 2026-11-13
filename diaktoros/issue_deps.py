"""Issue dependencies a maintainer declares (#671): issue B's fix waits for issue A's
implementation to be merged into the loop's base.

Declared only by the operator (``hermes dk depends``, or the Desktop's confirmed ``depends`` and
``waive`` actions, #1301), never inferred from issue text (#686, #708). This module owns the
rules for both: ``declare_problem``/``declare``, ``waive`` and ``listing`` (``row``); the CLI and
``one_shot_action`` only call them. A prerequisite counts as done only on evidence that its implementation is in the base:

- an issue-fix PR the ledger recorded for it, merged into the base; or
- the issue closed by a commit GitHub says the base contains (a ``Fixes #A`` merge).

Closing A any other way is not done: B keeps waiting until a maintainer waives the dependency.
An unreadable fact is never guessed: B waits, and the reason says GitHub could not be read. An
unreadable dependency record is never "none declared" either (#2150): the hold path reads it
strictly and holds every issue until it can be read again.

A waiver removes the dependency and records who waived it and when beside the record
(``issue-deps-waivers.json``, #1301); nothing that decides a hold reads the waivers.
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


def _answer(loop: dict, number: int) -> dict | None:
    """What GitHub answers for this repository's issue ``number``, unchecked; None unreadable."""
    data = gh.api(loop, f"/repos/{loop['repo']}/issues/{number}", login=loop["read_token"])
    return data if isinstance(data, dict) else None


def elsewhere(loop: dict, found: dict) -> bool:
    """Is ``found`` an issue of another repository? GitHub answers a transferred issue's old
    number with the issue where it now lives; one that does not name its repository is not."""
    url = found.get("repository_url")
    return (url is not None and not (isinstance(url, str) and url.rstrip("/").lower().endswith(
        "/repos/" + loop["repo"].lower())))


def read_issue(loop: dict, number: int) -> dict | None:
    """The issue as the reader sees it; None when unreadable, or when the answer is another
    number or another repository's issue (never joined, never read as this one's state)."""
    data = _answer(loop, number)
    return data if data and data.get("number") == number and not elsewhere(loop, data) else None


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
    ``(False, why not yet)`` or ``(None, what could not be read)``. The issue is read first, so
    another repository's issue (a transferred one) is never met, by any evidence (#2399 F8)."""
    base = loop["base"]
    issue = read_issue(loop, prerequisite)
    if issue is None:
        return None, f"#{prerequisite} could not be read"
    for pr in _fix_prs(loop, prerequisite):
        data = gh.api(loop, gh.pr_path(loop, pr), login=loop["read_token"])
        if not isinstance(data, dict) or data.get("number") != pr:
            return None, f"#{prerequisite}'s fix PR #{pr} could not be read"
        if data.get("merged_at") and (data.get("base") or {}).get("ref") == base:
            return True, f"#{prerequisite}'s fix PR #{pr} merged into {base}"
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


# -- the one owner of declare, waive and list (#1301): the CLI and the Desktop both call these ----

STATES = {True: "met", False: "waiting", None: "unknown"}
# Who declared (or waived) a dependency: the CLI writes "operator", the Desktop's confirmed
# action "desktop".
BY_CLI, BY_DESKTOP = "operator", "desktop"
# The one sentence for a dependency record that cannot be read (#2150): bounded, no path and no
# exception text. ``blocking`` answers it in place of a prerequisite number (``None``).
UNREADABLE = "the issue dependency record could not be read; held until it can be read"


class RecordUnreadable(Exception):
    """The dependency record (or its waiver history) could not be read or written: the question
    cannot be answered, and nothing is guessed (``hermes dk depends`` exits 2)."""

    def __init__(self):
        super().__init__("the issue dependency record could not be read or written")


def _strict(read):
    """``read(strict=True)``, any failure as ``RecordUnreadable``."""
    try:
        return read(strict=True)
    except Exception:
        raise RecordUnreadable() from None


def declared(st, issue: int | None = None, *, strict: bool = False) -> list[tuple[int, int, dict]]:
    """``(issue, prerequisite, record)`` for every declared dependency (or ``issue``'s), sorted.
    ``strict`` raises on an unreadable record instead of reading it as none."""
    deps = st.issue_deps(strict=True) if strict else st.issue_deps()
    if issue is not None:
        deps = {issue: deps.get(issue) or {}}
    return [(b, a, dict(prerequisites[a])) for b, prerequisites in sorted(deps.items())
            for a in sorted(prerequisites)]


def row(loop: dict, issue: int, prerequisite: int, record: dict, *, guarded: bool = False,
        cache: dict | None = None) -> dict:
    """One dependency and whether it is met: ``{issue, on, state, why, by, at}``, ``state`` one
    of ``met``/``waiting``/``unknown`` (``met``'s answer). ``guarded`` turns an exception into
    ``unknown`` with a fixed sentence (never the exception's text); ``cache`` shares one ``met``
    answer per prerequisite across rows of one read."""
    if cache is not None and prerequisite in cache:
        done, why = cache[prerequisite]
    else:
        if guarded:
            try:
                done, why = met(loop, prerequisite)
            except Exception:                         # never guessed, never echoed
                done, why = None, f"#{prerequisite} could not be read"
        else:
            done, why = met(loop, prerequisite)
        if cache is not None:
            cache[prerequisite] = (done, why)
    return {"issue": issue, "on": prerequisite, "state": STATES[done], "why": why,
            "by": record.get("by"), "at": record.get("at")}


def listing(loop: dict, st, issue: int | None = None, *, guarded: bool = False) -> list[dict]:
    """Every declared dependency (or ``issue``'s) with whether it is met — what ``hermes dk
    depends`` with neither ``--on`` nor ``--waive`` prints. An unreadable record raises
    ``RecordUnreadable``: never listed as none declared (#2150). ``guarded`` as ``row``'s."""
    try:
        pairs = declared(st, issue, strict=True)
    except Exception:
        raise RecordUnreadable() from None
    cache: dict = {}
    return [row(loop, b, a, record, guarded=guarded, cache=cache) for b, a, record in pairs]


# The sentences ``hermes dk depends`` prints (after its ``[loop]`` prefix) and ``explain`` shows.
NOT_LISTED = ("the issue dependency record could not be read — nothing listed; this is not a "
              "claim that none are declared")
WAIVERS_UNREADABLE = "waiver history could not be read"


def sentences(loop: dict, st, issue: int | None = None, *, guarded: bool = False
              ) -> tuple[list[str], list[str]]:
    """``(dependencies, waivers)`` as sentences: each declared dependency (or ``issue``'s) with
    whether it is met, then each waiver; a waiver history that cannot be read is the one line
    ``WAIVERS_UNREADABLE``. The one source of both ``hermes dk depends``'s list and ``explain``'s
    lines (#1301). An unreadable record raises ``RecordUnreadable``."""
    deps = [f"#{e['issue']} depends on #{e['on']}: {e['state']} — {e['why']}"
            for e in listing(loop, st, issue, guarded=guarded)]
    try:
        waivers = [f"#{w['issue']} no longer waits on #{w['on']} — waived by "
                   f"{w['by'] or 'unknown'} at {w['at'] or 'unknown'}" for w in waived(st, issue, strict=True)]
    except Exception:
        waivers = [WAIVERS_UNREADABLE]
    return deps, waivers


def explain_lines(loop: dict, st, issue: int) -> list[str]:
    """``explain``'s lines for ``issue`` (#1301): exactly what ``hermes dk depends --issue N``
    lists for it, without the "none declared" line; a read GitHub could not answer is
    ``unknown`` (never its error text), an unreadable record ``NOT_LISTED``."""
    try:
        deps, waivers = sentences(loop, st, issue, guarded=True)
    except RecordUnreadable:
        return [NOT_LISTED]
    return deps + waivers


def waived(st, issue: int | None = None, *, strict: bool = False) -> list[dict]:
    """``{issue, on, by, at}`` for every recorded waiver (or those of ``issue``), sorted. A state
    written before waivers were kept has none. ``strict`` raises on an unreadable history (or
    dependency record). A waiver whose pair is declared again is not shown: the dependency
    holds, so it is never "waived" and "waiting" at once (#2399 review F2)."""
    waivers = st.issue_dep_waivers(strict=True) if strict else st.issue_dep_waivers()
    deps = st.issue_deps(strict=True) if strict else st.issue_deps()
    if issue is not None:
        waivers = {issue: waivers.get(issue) or {}}
    return [{"issue": b, "on": a, "by": record.get("by"), "at": record.get("at")}
            for b, prerequisites in sorted(waivers.items()) for a, record in sorted(prerequisites.items())
            if a not in (deps.get(b) or {})]


def declare_problem(loop: dict, st, issue: int, prerequisite: int) -> tuple[int, str]:
    """Why ``issue`` may not depend on ``prerequisite`` now: ``(0, "")`` when it may, ``(1, why)``
    when refused (itself, a cycle, a pull request, an issue GitHub answers from another
    repository), ``(2, why)`` when it cannot be answered (an issue GitHub would not read). Both
    numbers are read in this loop's repository, and an answer naming another repository (a
    transferred issue) is refused, so a number of another repository is never joined. An
    unreadable record raises ``RecordUnreadable``: no cycle is checked against a guess."""
    reason = problem(_strict(st.issue_deps), issue, prerequisite)
    if reason:
        return 1, reason
    for number in (issue, prerequisite):
        found = _answer(loop, number)
        if found is not None and elsewhere(loop, found):        # transferred away (#1301)
            return 1, (f"#{number} is an issue of another repository; a dependency joins two "
                       "issues of this repository")
        if found is None or found.get("number") != number:
            return 2, f"#{number} could not be read from GitHub — nothing declared"
        if "pull_request" in found:
            return 1, (f"#{number} is a pull request; a dependency joins two issues of this "
                       "repository")
    return 0, ""


def declare(loop: dict, st, issue: int, prerequisite: int, by: str = BY_CLI
            ) -> tuple[int, str, bool]:
    """Check (``declare_problem``) and record that ``issue`` waits on ``prerequisite``:
    ``(code, why, added)``; ``added`` is False when it was already declared (nothing written)."""
    from .util import now_iso
    code, reason = declare_problem(loop, st, issue, prerequisite)
    if code:
        return code, reason, False
    try:
        added = st.issue_dep_add(issue, prerequisite, {"by": by, "at": now_iso()})
    except Exception:
        raise RecordUnreadable() from None
    return 0, "", added


def waive(st, issue: int, prerequisite: int, by: str = BY_CLI) -> str:
    """Record who waived the dependency and when, then remove it: ``""`` when there was none,
    ``"waived"``, or ``"unrecorded"`` when it was removed but the history write failed (said so,
    never reported as recorded). The next sweep releases ``issue`` if nothing else holds it. An
    unreadable record, a removal that did not land, or a failed history write over an older
    waiver of the pair raises ``RecordUnreadable`` and nothing is removed: the dependency still
    holds, no waiver is shown, and an older one is never shown as this one (#2399 F2, F6).
    """
    from .util import now_iso
    try:
        done = st.issue_dep_drop(issue, prerequisite, {"by": by, "at": now_iso()})
    except Exception:
        raise RecordUnreadable() from None
    return {"": "", "dropped": "waived"}.get(done, "unrecorded")


def blocking(loop: dict, st, issue: int) -> list[tuple[int | None, str]]:
    """The prerequisites of ``issue`` not yet done (or unreadable), with why: ``[]`` lets it go.

    The record is read strictly (#2150): when it cannot be read the answer is one blocker,
    ``(None, UNREADABLE)``, so every issue is held, never released, until it can. Waivers are
    not read here: a waived dependency is no longer in the record and never blocks."""
    try:
        deps = st.issue_deps(strict=True)
    except Exception:
        return [(None, UNREADABLE)]
    out = []
    for prerequisite in sorted((deps.get(issue) or {})):
        try:
            done, why = met(loop, prerequisite)
        except Exception as exc:                      # never guessed: an error blocks
            done, why = None, f"#{prerequisite}: {type(exc).__name__}: {exc}"
        if done is not True:
            out.append((prerequisite, why))
    return out


def hold_reason(loop: dict, issue: int) -> str:
    """Why a *queued* fix for ``issue`` may not launch or publish now, or ``""`` (#775).

    The same ``blocking`` rule the hand-off uses, re-read from the current declarations, so a
    dependency declared after the turn was queued still holds it and a waiver releases it. An
    unreadable ledger or fact holds too; it is never taken as met.
    """
    from . import state as state_mod
    try:
        blockers = blocking(loop, state_mod.state_for(loop), issue)
    except Exception as exc:
        return f"issue dependencies unreadable ({type(exc).__name__}); held"
    if not blockers:
        return ""
    return "waiting for " + "; ".join(why if a is None else f"#{a} ({why})" for a, why in blockers)


def names(blockers: list[tuple[int | None, str]]) -> str:
    """``#5, #6`` for ``blocking``'s answer, or that the record is unreadable."""
    if any(a is None for a, _ in blockers):
        return "its dependency record (unreadable)"
    return ", ".join(f"#{a}" for a, _ in blockers)


def wait(loop: dict, st, issue: int, blockers: list[tuple[int, str]]) -> None:
    """Record that ``issue``'s hand-off waits, and say so once on the issue. A wait on an
    unreadable record (#2150) is a local fault, not a maintainer's dependency: it is recorded
    (the sweep re-checks it) and logged, and nothing is posted."""
    from . import config
    st.dep_wait_set(issue)
    if any(a is None for a, _ in blockers):
        from .util import log
        log(f"issue #{issue}: {UNREADABLE}")
        return
    if not st.dep_wait_say(issue):                    # claimed under the lock: only one process posts
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
            st.dep_wait_unsay(issue)                  # not said: a later sweep retries
