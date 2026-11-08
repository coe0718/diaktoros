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
"""
from __future__ import annotations

import time

from . import ledger

PREFIX = "DECLINE:"           # the request's verdict field: ``DECLINE:<kind>``
# Each kind, and the maintainer's move it leaves.
KINDS = {"size-limit": "split the file over its limit, or raise its baseline",
         "not-this-pr": "re-run the failed job, or fix what broke outside this PR"}


def kind(verdict: object) -> str:
    """The kind a request's verdict declines with (``DECLINE:<kind>``), ``""`` for no decline.
    An unknown kind is ``ValueError``."""
    if not isinstance(verdict, str) or not verdict.startswith(PREFIX):
        return ""
    name = verdict[len(PREFIX):]
    if name not in KINDS:
        raise ValueError(f"a decline's kind is one of {', '.join(KINDS)}")
    return name


def comment(name: str, body: str) -> str:
    """The text posted as the fixer's answers: the decline, its reason, and whose move is next."""
    return (f"**CI fix declined ({name}): no push.**\n\n{body.strip()}\n\n"
            f"Next, for a maintainer: {KINDS[name]}. A new head gets a CI fix again.")


def record(db, run_id: str, repo: str, pr: int, head: str, name: str, body: str) -> None:
    """Record the decline before its comment is posted: one per run."""
    with ledger.connect(db, timeout=ledger.LOCK_WAIT_S) as con:
        con.execute("INSERT INTO declines(run_id,repo,pr,head,kind,body,created) "
                    "VALUES(?,?,?,?,?,?,?)", (run_id, repo, pr, head, name, body, time.time()))


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
    """The one urgent ``ci_declined`` notice (keyed by the run: never twice). Best effort."""
    from . import observer, state as state_mod
    try:
        observer.notify(loop, state_mod.state_for(loop), "ci_declined", number, head,
                        identity=run_id, outcome=summary(name, body)[:600], next_turn="you")
    except Exception:  # noqa: BLE001 - observer.notify never raises; the state may
        pass


def line(db, repo: str, pr: int, head: str, short: str) -> str:
    """``explain``'s blocker for a declined CI fix at this head, or ``""``."""
    found = at_head(db, repo, pr, head)
    return (f"the CI fix at head {short} was {summary(*found)}; nothing retries it, and the "
            "review waits for green or a new head") if found else ""
