"""The ``diaktoros/review`` merge check's state (#1163 M2b): what the loop says about a PR head,
as GitHub's merge button understands it.

``ready`` already decides whether a head may merge; this turns its reasons (``ready.assess_kinds``)
into one check state. Posting it is separate (a check run from the reviewer App, or a commit status
from the reviewer's PAT): this module reads and decides, it writes nothing.

* **success**: nothing the loop knows holds the head.
* **failure**: something needs a change or a person — changes requested at the head, red required
  CI, a conflict, a stale approval, an open gate failure, a human-paths hold, the shadow's hold or a
  ruling the operator decides.
* **pending**: everything left can still clear on its own — a review, the shadow, CI or a ruling
  still out, a draft, or a read that failed and will be retried.

GitHub's ``blocked`` is left out only while the base may require this check (#1227): with the
check required, GitHub reports ``blocked`` *because* the check is not green yet, and counting that
would keep the check from ever turning green. When the base is known not to require it,
``blocked`` is some other rule (required reviews, another required check), so it counts as a wait
(pending), as ``ready`` excludes the PR for it, instead of reading success while the merge button
refuses. A base whose protection could not be fully read (the classic protection read needs admin
access) is taken as requiring the check: guessing wrong that way only reads green early, the other
way deadlocks. (GitHub's other verdicts always count: a conflict or ``behind`` needs a change, an
uncomputed state is pending.) The per-PR reasons come from ``ready.build_kinds``, stacking
included. A PR outside the loop gets no check at all.
"""
from __future__ import annotations

import contextlib
import fcntl
import os

from . import ci, ready, util

NAME = ci.MERGE_CHECK
SUCCESS, PENDING, FAILURE = "success", "pending", "failure"
SUMMARY_MAX = 60_000          # GitHub's check-run summary allows 65535 characters


def decide(kinds: list[tuple[str, str]], required: bool = True) -> tuple[str | None, str]:
    """``(state, summary)`` from ``ready.assess_kinds``' reasons; state ``None`` when the PR is not
    the loop's (no check is posted). ``required`` (``base_requires_check``): whether the base may
    require this check; only then is GitHub's ``blocked`` left out, else it is a wait."""
    if any(kind == ready.OUTSIDE for kind, _ in kinds):
        return None, "not a loop PR"
    counted = [(ready.PENDING if kind == ready.GITHUB else kind, text) for kind, text in kinds
               if kind != ready.GITHUB or not required]
    failing = [text for kind, text in counted if kind == ready.FAILURE]
    waiting = [text for kind, text in counted if kind != ready.FAILURE]
    if failing:
        state = FAILURE
    elif waiting:
        state = PENDING
    else:
        return SUCCESS, "Approved at this head; nothing the loop knows holds it."
    lines = ([f"- {text}" for text in failing]
             + ([""] if failing and waiting else [])
             + ([f"- (waiting) {text}" for text in waiting] if failing else
                [f"- {text}" for text in waiting]))
    head = ("Blocked: what has to change, then what is still in progress."
            if state == FAILURE else "In progress: nothing needs a change yet.")
    return state, (head + "\n\n" + "\n".join(lines))[:SUMMARY_MAX]


def base_requires_check(loop: dict) -> bool:
    """Whether GitHub's ``blocked`` may be the base waiting on this check (#1227): True when the
    base requires ``diaktoros/review`` or a read of its protection did not answer; False only when
    both reads answered and neither requires it."""
    from . import protection
    found = protection.read(loop)
    return found.required or bool(found.unread)


def _blocked_counts(loop: dict, kinds: dict) -> bool:
    """``decide``'s ``required`` for one refresh: the base is read only when some PR carries
    GitHub's ``blocked``, and once for all of them."""
    if not any(kind == ready.GITHUB for reasons in kinds.values() for kind, _ in reasons):
        return True
    return base_requires_check(loop)


def title(state: str) -> str:
    """The one-line title GitHub shows next to the check."""
    return {SUCCESS: "Ready to merge", FAILURE: "Not mergeable",
            PENDING: "Waiting"}.get(state, "Waiting")


# -- posting (M2b-2) ---------------------------------------------------------------------------
#
# The host posts the state with the reviewer seat's credential: a check run when the reviewer is a
# GitHub App (it needs Checks: write; branch protection can pin the check to that App), else a
# commit status (the reviewer's PAT needs repo:status; it cannot be pinned). One record per PR in
# the loop's state dir keeps a write to a real change: a new head, state or summary.

FILE = "merge-check.json"
STATUS_DESCRIPTION_MAX = 140    # GitHub's commit-status description limit


LOCK = util.register_lock("merge-check.lock")


@contextlib.contextmanager
def _writer(st, wait: bool = True):
    """The one merge-check writer for this loop (#1251): held from reading what was posted through
    the POST to recording it, so two writers never both decide nothing is posted for a head and
    both create a run. Taken before the state lock, never inside it. Yields False when ``wait`` is
    off and another writer holds it."""
    st.dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(st.dir / LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:                                # closing the descriptor releases the lock
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def _digest(state: str, summary: str) -> str:
    import hashlib
    return hashlib.sha256(f"{state}\n{summary}".encode()).hexdigest()[:16]


def post(loop: dict, number: int, head: str, state: str, summary: str,
         run_id: int | None = None) -> tuple[int | None, str]:
    """Write one check (``(run id, "")``) or commit status (``(None, "")``) for ``head``; on a failed
    write ``(run_id, why)``. A check run already posted for this head is updated, not duplicated."""
    from . import config, gh
    login = config.seat_login(loop, "reviewer")
    repo = loop["repo"]
    if config.reviewer_is_app(loop):
        body = {"name": NAME, "head_sha": head,
                "output": {"title": title(state), "summary": summary}}
        if state == PENDING:
            body["status"] = "in_progress"
        else:
            body.update(status="completed", conclusion=state)
        if run_id:
            done = gh.api(loop, f"/repos/{repo}/check-runs/{run_id}", method="PATCH",
                          body={k: v for k, v in body.items() if k != "head_sha"}, login=login)
            if isinstance(done, dict) and done.get("id") == run_id:
                return run_id, ""
            if not gh.last_error().startswith("HTTP 404"):
                # #1230: only a run that is gone gets a new one; any other failure is retried on
                # the next refresh, so a head never carries two runs of the check.
                return run_id, gh.last_error() or "check run not updated"
        done = gh.api(loop, f"/repos/{repo}/check-runs", method="POST", body=body, login=login)
        if isinstance(done, dict) and type(done.get("id")) is int:
            return done["id"], ""
        return run_id, gh.last_error() or "check run not written"
    reasons = [line[2:] for line in summary.splitlines() if line.startswith("- ")]
    first = reasons[0] if reasons else summary.splitlines()[0] if summary else ""
    body = {"state": state, "context": NAME,             # a status shows one line: the first reason
            "description": (f"{title(state)}: {first}" if first else title(state))
            [:STATUS_DESCRIPTION_MAX]}
    done = gh.api(loop, f"/repos/{repo}/statuses/{head}", method="POST", body=body, login=login)
    if isinstance(done, dict) and done.get("context") == NAME:
        return None, ""
    return None, gh.last_error() or "commit status not written"


def refresh(loop: dict, st, only: set[int] | None = None, log=lambda _msg: None) -> int:
    """Post the merge check for every open loop PR (or those in ``only``) whose state changed since
    it was last posted; the number written. Nothing is read or written while the check is off. An
    unreadable PR listing writes nothing (never a guess). With ``only``, only those PRs (and the
    PRs they are stacked on) are read (#1645)."""
    from . import config
    if not config.merge_check_on(loop):
        return 0
    import time
    with st.locked():                   # what the store held before the listing (#1260)
        before = st.kv_get(st.dir / FILE, {})
    before = before if isinstance(before, dict) else {}
    prs, kinds, error = ready.collect_kinds(loop, st, only)
    if error:
        log(f"merge check not refreshed: {error}")
        return 0
    path = st.dir / FILE
    written = 0
    required = _blocked_counts(loop, kinds)
    for pr in prs:
        number = pr["number"]
        head = (pr.get("head") or {}).get("sha") or ""
        if not head or (only is not None and number not in only):
            continue
        state, summary = decide(kinds.get(number) or [], required)
        if state is None:
            continue                                   # not a loop PR: no check
        digest = _digest(state, summary)
        with _writer(st):
            with st.locked():
                memory = st.kv_get(path, {})
                memory = memory if isinstance(memory, dict) else {}
                last = memory.get(str(number)) if isinstance(memory.get(str(number)), dict) else {}
            if last.get("head") == head and last.get("digest") == digest:
                continue
            run_id = last.get("run_id") if last.get("head") == head else None
            run_id, why = post(loop, number, head, state, summary, run_id=run_id)
            if why:
                log(f"#{number} @ {head[:7]}: merge check not posted ({why})")
                continue
            with st.locked():
                memory = st.kv_get(path, {})
                memory = memory if isinstance(memory, dict) else {}
                memory[str(number)] = {"head": head, "state": state, "digest": digest,
                                       "at": time.time(), **({"run_id": run_id} if run_id else {})}
                # Drop only what the listing shows gone and was already there, unchanged, before
                # it (#1258): a pending mark made mid-refresh, for a PR the listing predates, is
                # kept. Compared by content, not by wall-clock stamps, so a clock stepped back
                # between the two reads cannot make a later mark look older (#1260).
                open_numbers = {str(p["number"]) for p in prs}
                memory = {k: v for k, v in memory.items()
                          if k in open_numbers or k not in before or v != before[k]}
                st.kv_set(path, memory)
        written += 1
        log(f"#{number} @ {head[:7]}: merge check {state}")
    return written


def has_head(st, number: int, head: str) -> bool:
    """Whether ``merge-check.json`` already holds ``head`` for this PR: the check was decided
    (or marked pending) for it, so nothing new has to be posted on its account."""
    with st.locked():
        memory = st.kv_get(st.dir / FILE, {})
    last = memory.get(str(number)) if isinstance(memory, dict) else None
    return isinstance(last, dict) and last.get("head") == head


def mark_pending(loop: dict, st, number: int, head: str, why: str) -> bool:
    """Post ``pending`` for a head nothing has been posted for yet (#1242): a PR entering the loop
    shows the check at once, not after the first refresh. One write, no PR listing, so it is cheap
    enough for a gate; the next ``refresh`` decides the real state and updates the same run.
    Best effort: True only when it wrote."""
    import time
    from . import config, gh
    try:
        if not config.merge_check_on(loop) or not head:
            return False
        path = st.dir / FILE
        with _writer(st, wait=False) as mine:
            if not mine:
                return False                # a refresh is posting now: it writes the real state
            if has_head(st, number, head):
                return False                           # already posted for this head
            # Only what is known without reading the PR: the next refresh gives the real reasons.
            # A bullet, so a commit status reads "Waiting: review queued; …" (#1258), not twice.
            summary = (f"Waiting.\n\n- {why}; the full check follows once the loop reads this "
                       "head")
            run_id, failed = post(loop, number, head, PENDING, summary)
            if failed:
                return False
            with st.locked():
                memory = st.kv_get(path, {})
                memory = memory if isinstance(memory, dict) else {}
                memory[str(number)] = {"head": head, "state": PENDING, "at": time.time(),
                                       "digest": _digest(PENDING, summary),
                                       **({"run_id": run_id} if run_id else {})}
                st.kv_set(path, memory)
            return True
    except (Exception, gh.GateBudgetExceeded):
        # Never changes what the gate decided (shadow review of #1249): the turn is already armed,
        # so a gate budget spent on this extra write must not end the gate as a failure.
        return False


def refresh_new_head(loop: dict, st, number: int, head: str) -> None:
    """A gate declined an event at ``head`` (#1797): post the check for it now, with the reason
    the loop gives, instead of leaving the head without one until the next sweep. Best effort,
    like ``refresh_quietly``; nothing is read or written when the check is off or already holds
    this head, so a redelivered event costs nothing."""
    from . import config, gh
    try:
        if not config.merge_check_on(loop) or has_head(st, number, head):
            return
        refresh_quietly(loop, number)
    except (Exception, gh.GateBudgetExceeded):
        pass                   # a gate's spent budget must not turn its decision into a failure


def refresh_quietly(loop: dict, number: int | None = None) -> None:
    """``refresh`` after an event (a verdict, a shadow review, a ruling): best effort, never raises;
    the watchdog's sweep refreshes every open PR anyway."""
    try:
        from . import state as state_mod
        from .util import log
        refresh(loop, state_mod.state_for(loop), only=None if number is None else {number},
                log=log)
    except Exception as exc:              # a check that could not be posted never fails the event
        try:
            from .util import log
            log(f"merge check refresh failed: {type(exc).__name__}: {exc}")
        except Exception:
            pass
