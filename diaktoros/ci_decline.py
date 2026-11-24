"""A CI-fix turn that declines to push (#1526): a failure no push of this PR's should fix.

The CI-fix prompt tells the fixer not to push in two cases: the failure is not in the PR's code (a
flaky job, an outage, a workflow problem), or all that fails is a file over a size limit, which is
a maintainer's call (``prompts.size_limit_rule``). Before this, the turn's reason went to its
summary, which nothing publishes, and the turn ended unpublished: recorded as failed and retried, a
decision that cannot change re-run up to four times.

The fixer now ends such a turn with ``request_review --answers-file F --decline KIND``. The broker
takes it only on a CI-fix turn that pushed nothing (``kind``), records it here before anything is
posted (``record``), posts the reason once on the PR as the fixer's answers, sends one urgent
``ci_declined`` notice (``notify``), and the run ends succeeded, so nothing retries it. The answers
record makes it a write the ledger never replays: a redelivery is refused, and so is ``retry``.
A new head is what gets another CI fix. ``explain`` names it for its head (``line``).

A conflict-resolution turn declines the same way (#2034), with its own kind, ``needs-a-person``:
a conflicted file it cannot resolve honestly (it cannot tell what one side meant). Each kind is
accepted on its own scope only (``refusal``): a CI-fix kind on a CI-fix turn, a conflict kind on a
conflict turn (the broker scope's ``merge``). A conflict decline is also remembered for the head
(``remember``, #1154's record), so the next base merge queues no new conflict turn for it; like a
whole-file conflict it lapses after a day (#1271), and a new head clears it.
"""
from __future__ import annotations

import time

from .state import ledger

PREFIX = "DECLINE:"           # the request's verdict field: ``DECLINE:<kind>``
# Each kind, and the maintainer's move it leaves.
KINDS = {"size-limit": "split the file over its limit, or raise its baseline",
         "not-this-pr": "re-run the failed job, or fix what broke outside this PR",
         "needs-a-person": "merge the base by hand, resolving the files named above"}
# The kinds a conflict-resolution turn declines with (#2034); every other kind is a CI fix's.
CONFLICT_KINDS = frozenset({"needs-a-person"})


def kind(verdict: object) -> str:
    """The kind a request's verdict declines with (``DECLINE:<kind>``), ``""`` for no decline.
    An unknown kind is ``ValueError``."""
    if not isinstance(verdict, str) or not verdict.startswith(PREFIX):
        return ""
    name = verdict[len(PREFIX):]
    if name not in KINDS:
        raise ValueError(f"a decline's kind is one of {', '.join(KINDS)}")
    return name


def refusal(name: str, *, ci_fix: bool, conflict: bool) -> str:
    """Why a turn of this scope may not decline with ``name``, or ``""``. A conflict kind is a
    conflict-resolution turn's only, every other kind a CI-fix turn's only (#2034)."""
    if name in CONFLICT_KINDS:
        return "" if conflict else f"only a conflict-resolution turn may decline as {name}"
    return "" if ci_fix else f"only a CI-fix turn may decline as {name}"


def _what(name: str) -> str:
    """What a decline of this kind declined: the CI fix, or the conflict resolution."""
    return "conflict resolution" if name in CONFLICT_KINDS else "CI fix"


def comment(name: str, body: str) -> str:
    """The text posted as the fixer's answers: the decline, its reason, and whose move is next."""
    again = ("A new head (the base merged in) ends it." if name in CONFLICT_KINDS
             else "A new head gets a CI fix again.")
    what = _what(name)                 # not str.capitalize(): it would lowercase "CI"
    return (f"**{what[:1].upper()}{what[1:]} declined ({name}): no push.**\n\n{body.strip()}\n\n"
            f"Next, for a maintainer: {KINDS[name]}. {again}")


def record(db, run_id: str, repo: str, pr: int, head: str, name: str, body: str) -> None:
    """Record the decline before its comment is posted: one per run."""
    with ledger.connect(db, timeout=ledger.LOCK_WAIT_S) as con:
        con.execute("INSERT INTO declines(run_id,repo,pr,head,kind,body,created) "
                    "VALUES(?,?,?,?,?,?,?)", (run_id, repo, pr, head, name, body, time.time()))


def forget(db, run_id: str) -> None:
    """Withdraw a run's decline whose answers comment was definitely not posted (authorization
    denied, or its answers record could not be written, so no POST was made): nothing was
    declined, so ``explain`` names no decline and the row is no longer a write record. Never
    called once a POST was sent (posted, or uncertain). Best effort."""
    try:
        with ledger.connect(db, timeout=ledger.LOCK_WAIT_S) as con:
            con.execute("DELETE FROM declines WHERE run_id=?", (run_id,))
    except Exception:  # noqa: BLE001 - the run still fails on the comment's outcome
        pass


def at_head(db, repo: str, pr: int, head: str) -> tuple[str, str] | None:
    """``(kind, reason)`` of the newest decline at this head, or None (none, or no table yet)."""
    try:
        with ledger.connect(db, timeout=ledger.LOCK_WAIT_S) as con:
            row = con.execute("SELECT kind, body FROM declines WHERE repo=? AND pr=? AND head=? "
                              "ORDER BY created DESC LIMIT 1", (repo, pr, head)).fetchone()
    except Exception:  # noqa: BLE001 - an unreadable or older ledger has no decline to show
        return None
    return (row[0], row[1]) if row else None


def summary(name: str, body: str) -> str:
    """One line for a notice or ``explain``: the kind, the reason's start, the maintainer's move."""
    reason = " ".join(body.split())
    reason = reason if len(reason) <= 200 else reason[:200] + "…"
    return f"declined ({name}): {reason} — next: {KINDS[name]}"


def notify(loop: dict, number: int, head: str, run_id: str, name: str, body: str) -> None:
    """The one urgent ``ci_declined`` notice (keyed by the run: never twice), for a CI fix's or a
    conflict turn's decline alike (#2034). Best effort."""
    from . import observer, state as state_mod
    try:
        observer.notify(loop, state_mod.state_for(loop), "ci_declined", number, head,
                        identity=run_id, outcome=summary(name, body)[:600], next_turn="you")
    except Exception:  # noqa: BLE001 - observer.notify never raises; the state may
        pass


def remember(loop: dict, number: int, head: str, name: str, body: str) -> None:
    """A conflict decline makes this head's conflict a person's (#1154's record, #2034): the
    watchdog queues no new conflict turn for it on the next base merge. It lapses like a
    whole-file conflict's (#1271), since a later base may resolve it; a new head clears it. Only
    a conflict kind is remembered. Best effort."""
    if name not in CONFLICT_KINDS:
        return
    from . import conflict_person, state as state_mod
    try:
        conflict_person.record(state_mod.state_for(loop), int(number), head,
                               f"the fixer declined to resolve it: {summary(name, body)}",
                               lapse_s=conflict_person.WHOLE_FILE_RETRY_S)
    except Exception:  # noqa: BLE001 - the decline is recorded and posted; this only saves turns
        pass


def line(db, repo: str, pr: int, head: str, short: str) -> str:
    """``explain``'s blocker for a declined CI fix or conflict resolution at this head, or
    ``""``."""
    found = at_head(db, repo, pr, head)
    if not found:
        return ""
    waits = ("the PR waits for a person to merge the base (a new head)"
             if found[0] in CONFLICT_KINDS else "the review waits for green or a new head")
    return (f"the {_what(found[0])} at head {short} was {summary(*found)}; nothing retries it, "
            f"and {waits}")
