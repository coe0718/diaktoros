"""A maintainer's reply wakes the fixer on an issue it commented on instead of fixing (#663).

An issue-fix turn whose one write was a comment (a question, "needs a decision", "could not fix")
left the issue quiet: only the fix label, removed and added again, started another turn. Now the
watchdog sweep (``fix_hold.sweep``) queues the next turn once a maintainer replies, from the base
branch's current commit. An ask (``--ask``, #483) is resumed by ``fix_hold.sweep_asks`` instead.

A turn is queued only when all of these hold:

* the issue's latest issue-fix write is a posted comment, made within ``WAIT_DAYS``: a later PR
  from the issue ends the wait;
* no issue-fix turn for the issue is queued or running;
* the issue is open and still carries ``triage.fix_label`` (read through ``issue_fix_issue``);
* a comment by a maintainer, never by a seat's own login, was written after the issue's newest
  issue-fix turn was queued. One written during that turn wakes the next; several are one turn.

The new turn reads the issue's comments (#511), so it sees the reply. It is a maintainer's
hand-off, so ``auto_fix_daily`` does not count it.
"""
from __future__ import annotations

import time

from . import config, fix_hold, gate, gh
from .util import epoch, log

WAIT_DAYS = 14          # a comment older than this no longer waits for a reply
WAITING = "fix-replies.json"    # the issues waiting for a reply at the last sweep, for ``status``


def sweep(loop: dict, st, lines: list[str]) -> None:
    """Queue the fix of each issue a maintainer replied on; note the ones still waiting."""
    try:
        rows = list(gate.isolated_supervisor(loop).issue_fix_comment_waits(
            loop["repo"], time.time() - WAIT_DAYS * 86400))
    except Exception as exc:            # no ledger yet, or unreadable: the PR holds still run
        log(f"maintainer replies to issue-fix comments not checked: {type(exc).__name__}: {exc}")
        return
    waiting = {}
    for row in rows:
        number = row["number"]
        if row["active"] or number in st.fix_ask_open():
            continue                    # a turn will write again, or the ask's own sweep owns it
        try:
            outcome = wake(loop, st, row)
        except Exception as exc:        # retried on the next sweep
            lines.append(f"⚠️ issue #{number}: maintainer reply seen, fix not queued: "
                         f"{type(exc).__name__}: {exc}")
            continue
        if outcome == "waiting":
            waiting[str(number)] = row["comment_id"]
    st.kv_set(st.dir / WAITING, waiting)


def wake(loop: dict, st, row: dict) -> str:
    """``queued``, ``waiting`` (no reply yet), ``ineligible`` or ``unreadable`` for one issue."""
    from . import run_supervisor
    from .run_supervisor import issue_inputs
    number = row["number"]
    try:
        live = issue_inputs.issue_fix_issue(loop, number)
    except run_supervisor.RetryableError:
        return "unreadable"
    except Exception:                   # closed, not a fixable author, ...
        return "ineligible"
    labels = {str((x or {}).get("name") or "").casefold()
              for x in live.get("labels") or [] if isinstance(x, dict)}
    if loop["triage"]["fix_label"].casefold() not in labels:
        return "ineligible"             # a maintainer's label only, never an auto-offer
    if 0 < epoch(live.get("updated_at")) < row["began"]:
        return "waiting"                # nothing was written on it since: no comments read
    comments, error = gh.issue_comments_read(loop, number)
    if comments is None:
        log(f"issue #{number}: comments unreadable, reply check waits: {error}")
        return "unreadable"
    reply = newest_reply(loop, comments, row["began"])
    if reply is None:
        return "waiting"
    base, outcome = fix_hold.queue_fix(loop, number, f"issue-fix:reply{reply['id']}")
    login = str(reply["user"]["login"]).lower()
    log(f"issue #{number}: {login} replied to the fixer's comment; fix turn at {base[:7]}: "
        f"{outcome}")
    if outcome in ("enqueued", "rearmed", "pending"):
        from . import observer
        observer.notify(loop, st, "fixing", number, "", identity=f"reply{reply['id']}",
                        outcome=f"{login} replied; fix turn queued from {loop['base']} at "
                                f"{base[:7]}", issue=True, outcome_kind="queued")
    return "queued"


def newest_reply(loop: dict, comments: list, since: float) -> dict | None:
    """The newest maintainer comment written after ``since``, else ``None``. A seat's own
    comment never counts, even from a login that is also a maintainer."""
    seats = {config.seat_login(loop, "fixer").lower(), config.seat_login(loop, "reviewer").lower(),
             config.triage_login(loop)} - {""}
    allowed = config.maintainers(loop) - seats
    found = None
    for comment in comments:
        user = comment.get("user") if isinstance(comment, dict) else None
        login = str(user.get("login") or "").lower() if isinstance(user, dict) else ""
        if (type(comment.get("id")) is int and login in allowed
                and epoch(comment.get("created_at")) > since
                and (found is None or comment["id"] > found["id"])):
            found = comment
    return found


def status_lines(st) -> list[str]:
    """``status``'s and ``explain``'s lines for the issues waiting on a reply (the last sweep)."""
    waiting = st.kv_get(st.dir / WAITING, {})
    return [line(int(n)) for n in sorted(waiting if isinstance(waiting, dict) else {}, key=int)
            if str(n).isdigit()]


def line(number: int) -> str:
    return (f"issue #{number} fix waits for a maintainer's reply to the fixer's comment; a reply "
            "queues the next fix turn")
