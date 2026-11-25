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
  issue-fix turn was queued. One written during that turn wakes the next; several are one turn;
* no open PR's body says it fixes the issue, the guard ``fix_hold.hand_off`` applies (#2021).
  A reply seen while the open PR listing cannot be read is kept in ``GUARD`` and retried every
  sweep for up to ``WAIT_DAYS`` more, even once its comment is older than ``WAIT_DAYS`` and
  through any read that fails meanwhile; status says it waits on the listing, never on a reply,
  until a wake (the sweep's or the webhook's) reads it (#2021 review).

The new turn reads the issue's comments (#511), so it sees the reply. It is a maintainer's
hand-off, so ``auto_fix_daily`` does not count it. It starts ``reply_settle_seconds`` after it
is queued (#2248, ``fix_settle``): a reply followed by a close ends it before any model runs.

With ``comment_webhook`` on, the triage route and hook carry ``issue_comment`` and a maintainer's
comment does the same at once (``on_comment``, #680), for an ask too. The delivery is a nudge,
never the truth (#679): only the sender and the issue number are taken from it, the sender is
filtered before any read, and the rest is read again exactly as the sweep reads it. The sweep
stays the backstop for a delivery that never arrives.
"""
from __future__ import annotations

import time

from . import config, fix_hold, fix_settle, gate, gh
from . import fix_reply_state as kept
from .fix_reply_state import STATE, WAITING  # noqa: F401 - the files' names
from .util import epoch, log, silence

WAIT_DAYS = 14          # a comment older than this no longer waits for a reply


def sweep(loop: dict, st, lines: list[str]) -> None:
    """Queue the fix of each issue a maintainer replied on; note the ones still waiting. Each
    issue's decision is committed as ``fix_reply_state`` says: the newest reads decide."""
    read_at, now, guard = kept.stamp(), time.time(), kept.guard(st)
    try:
        sup = gate.isolated_supervisor(loop)
        rows = list(sup.issue_fix_comment_waits(loop["repo"], now - WAIT_DAYS * 86400))
        if guard:                       # a guarded reply outlives its comment's window
            seen = {row["number"] for row in rows}
            oldest = min(entry["at"] for entry in guard.values()) - WAIT_DAYS * 86400
            rows += [row for row in sup.issue_fix_comment_waits(loop["repo"], oldest)
                     if str(row["number"]) in guard and row["number"] not in seen]
    except Exception as exc:            # no ledger yet, or unreadable: the PR holds still run
        log(f"maintainer replies to issue-fix comments not checked: {type(exc).__name__}: {exc}")
        return
    listed = {str(row["number"]) for row in rows}
    for key in (set(kept.waiting(st)) | set(guard)) - listed:   # no comment waits on it now
        kept.commit(st, key, read_at, wait=None, seen=None)
    for row in rows:
        number, key = row["number"], str(row["number"])
        if row["active"] or number in st.fix_ask_open():
            # A turn will write again, or the ask's own sweep owns it.
            kept.commit(st, key, read_at, wait=None, seen=None)
            continue
        try:
            outcome = wake(loop, st, row, read_at)     # its row is from the ledger read (F10)
        except Exception as exc:        # retried on the next sweep; nothing it kept changes
            lines.append(f"⚠️ issue #{number}: maintainer reply seen, fix not queued: "
                         f"{type(exc).__name__}: {exc}")
            outcome = "failed"
        entry = kept.guard(st).get(key)
        # A known reply outlives any read that fails (#2549 review), up to WAIT_DAYS more.
        if entry and outcome in ("unlisted", "unreadable", "failed") \
                and now - entry["at"] >= WAIT_DAYS * 86400 \
                and kept.commit(st, key, kept.stamp(), wait=None, seen=None, expect_at=entry["at"]):
            lines.append(f"⚠️ issue #{number}: maintainer reply seen, but the open PR listing "
                         f"or the issue stayed unreadable for {WAIT_DAYS} days; fix not queued")


def wake(loop: dict, st, row: dict, read_at: int | None = None) -> str:
    """``queued``, ``waiting`` (no reply yet), ``ineligible``, ``unreadable``, or ``unlisted``
    (a reply, but the open PR listing could not be read: kept in ``GUARD``) for one issue,
    committed from reads begun at ``read_at`` (the caller's ledger read that found ``row``; now
    when not given), for the sweep and the webhook alike. What it could not read
    (``unreadable``, or an exception) changes nothing kept: a wait stays, none is made (#2019)."""
    read_at, key = kept.stamp() if read_at is None else read_at, str(row["number"])
    outcome = _wake(loop, st, row)
    if outcome in ("queued", "ineligible"):     # no longer waits, on a reply or the listing
        kept.commit(st, key, read_at, wait=None, seen=None)
    elif outcome == "waiting":
        kept.commit(st, key, read_at, wait=row["comment_id"], seen=None)
    elif outcome == "unlisted":
        kept.commit(st, key, read_at, seen={"at": time.time()})
    return outcome


def _wake(loop: dict, st, row: dict) -> str:
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
    # The guard hand_off and _release apply (#2021, #2022): never a second fix of an issue an
    # open PR already says it fixes. No comment here: the row is read again at every sweep.
    prs, error = gh.open_prs_read(loop)
    if prs is None:
        log(f"issue #{number}: open PR listing unreadable, reply wake waits: {error}")
        return "unlisted"
    existing = fix_hold._closing(loop, prs, number)
    if existing is not None:
        log(f"issue #{number}: open PR #{existing} already fixes it; reply wakes no fix turn")
        return "ineligible"
    base, outcome = fix_hold.queue_fix(loop, number, f"issue-fix:reply{reply['id']}", settle=True)
    login = str(reply["user"]["login"]).lower()
    wait = fix_settle.seconds(loop) if outcome in ("enqueued", "rearmed") else 0     # #2248
    starts = f", starts in {wait}s unless the issue closes" if wait else ""
    log(f"issue #{number}: {login} replied to the fixer's comment; fix turn at {base[:7]}: "
        f"{outcome}{starts}")
    if outcome in ("enqueued", "rearmed", "pending"):
        from . import observer
        observer.notify(loop, st, "fixing", number, "", identity=f"reply{reply['id']}",
                        outcome=f"{login} replied; fix turn queued from {loop['base']} at "
                                f"{base[:7]}{starts}", issue=True, outcome_kind="queued")
    return "queued"


def on_comment(loop: dict, payload: dict) -> None:
    """The triage gate's path for an ``issue_comment`` delivery (#680). Always ends in a silence."""
    from . import state as state_mod
    if not config.comment_webhook(loop):
        silence(f"issue_comment: comment_webhook is off for {loop['id']}; the watchdog sweep "
                "reads replies")
    if payload.get("action") != "created":
        silence(f"issue_comment {payload.get('action')!r}: only a new comment is acted on")
    sender = payload.get("sender")
    login = str(sender.get("login") or "").lower() if isinstance(sender, dict) else ""
    if login not in repliers(loop):
        silence(f"issue_comment by {login or 'an unknown login'}: not a maintainer's reply")
    issue = payload.get("issue")
    number = issue.get("number") if isinstance(issue, dict) else None
    if type(number) is not int or number < 1:
        silence("issue_comment: payload has no issue number")
    if "pull_request" in issue:
        on_pr_comment(loop, number, login)
    st = state_mod.state_for(loop)
    if number in st.fix_ask_open():                 # an ask (#483) resumes by its own rules
        lines: list[str] = []
        run = st.fix_ask_open()[number].get("run")
        dropped = fix_hold.sweep_asks(loop, st, lines, only=number)
        if number in dropped:                       # #2051, #2245: closed or no longer eligible
            silence(f"issue #{number}: ask dropped: {dropped[number]}")
        if number in st.fix_ask_open():
            silence(f"issue #{number}: the ask is still open " + " ".join(lines))
        if run:                                     # #2334: the plan `continue` posted
            silence(f"issue #{number}: {login} approved the plan of run {run}; `hermes dk "
                    f"continue --loop {loop['id']} --run {run}` opens its PR", "accepted")
        silence(f"issue #{number}: {login} answered the fixer's ask; fix turn resumed", "accepted")
    read_at = kept.stamp()                          # before the ledger read its row comes from
    rows = [row for row in gate.isolated_supervisor(loop).issue_fix_comment_waits(
        loop["repo"], time.time() - WAIT_DAYS * 86400) if row["number"] == number]
    if not rows or rows[0]["active"]:
        silence(f"issue #{number}: no fixer comment waits for a reply")
    try:
        outcome = wake(loop, st, rows[0], read_at)
    except Exception as exc:                        # the sweep retries it
        silence(f"issue #{number}: reply seen, fix not queued: {type(exc).__name__}: {exc}")
    if outcome != "queued":
        silence(f"issue #{number}: no fix turn queued ({outcome})")
    silence(f"issue #{number}: {login} replied; fix turn queued", "accepted")


def on_pr_comment(loop: dict, number: int, login: str) -> None:
    """#2025: a maintainer's comment on a loop PR re-queues the fixer turn waiting at its head:
    one that ended there without pushing (failed, waiting on backoff, policy-cancelled). It is
    re-armed exactly as `hermes dk retry --pr N --seat fixer` would (``retry_action.perform``:
    the same write-evidence and push-policy refusals), confirmed against the PR's live head.
    The comment reaches the turn as data, in the PR's maintainers' comments (#666). A
    review-only cap, a ruling and admission are not lifted by a comment. Always silences."""
    from . import retry_action
    pr = gh.pr(loop, number)
    head = ((pr.get("head") or {}).get("sha") or "") if isinstance(pr, dict) else ""
    if not isinstance(pr, dict) or pr.get("state") != "open" or not head:
        silence(f"#{number}: a comment on a PR that is not open or unreadable; nothing re-queued")
    result = retry_action.perform(loop, number, "fixer", confirm_head=head, read_pr=lambda: pr)
    if not result["rearmed"]:
        why = retry_action.bounded(result["message"] or "; ".join(
            str(e.get("reason") or "") for e in result["refused"]) or "nothing to re-arm")
        silence(f"#{number}: a comment on a PR; no fixer turn re-queued ({why}); the seats read "
                "it in their next turn (#666)")
    silence(f"#{number}: {login} commented; fixer turn at {head[:7]} re-queued", "accepted")


def repliers(loop: dict) -> set[str]:
    """The logins whose comment wakes the fixer: the maintainers, never a seat's own login, even
    one that is also listed as a maintainer."""
    seats = {config.seat_login(loop, "fixer").lower(), config.seat_login(loop, "reviewer").lower(),
             config.triage_login(loop)} - {""}
    return config.maintainers(loop) - seats


def newest_reply(loop: dict, comments: list, since: float) -> dict | None:
    """The newest maintainer comment written after ``since`` (``repliers``), else ``None``."""
    allowed = repliers(loop)
    found = None
    for comment in comments:
        user = comment.get("user") if isinstance(comment, dict) else None
        login = str(user.get("login") or "").lower() if isinstance(user, dict) else ""
        if (type(comment.get("id")) is int and login in allowed
                and epoch(comment.get("created_at")) > since
                and (found is None or comment["id"] > found["id"])):
            found = comment
    return found


def status_lines(st, only: int | None = None) -> list[str]:
    """``status``'s and ``explain``'s lines for the issues waiting on a reply (the last sweep), or
    on the open PR listing after a reply (``GUARD``); with ``only``, that issue's."""
    waiting = kept.waiting(st)
    guard = kept.guard(st)              # a known reply never also reads as awaited
    found = [(n, line) for n in (waiting if isinstance(waiting, dict) else {}) if n not in guard]
    found += [(n, guarded_line) for n in guard]
    # Filter, then sort: a key that is not a plain number is skipped, never a crash (#2020).
    return [say(int(n)) for n, say in sorted(found, key=lambda f: str(f[0]).zfill(20))
            if str(n).isascii() and str(n).isdigit() and only in (None, int(n))]


def guarded_line(number: int) -> str:
    return (f"issue #{number} fix waits for the open PR listing: a maintainer replied, but GitHub's "
            "list of open PRs could not be read, so whether a PR already fixes it is unknown")


def line(number: int) -> str:
    return (f"issue #{number} fix waits for a maintainer's reply to the fixer's comment; a reply "
            "queues the next fix turn")
