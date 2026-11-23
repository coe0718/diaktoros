"""Recovery sweep for issue triage whose webhook delivery never arrived (#849).

The Hermes webhook gateway answers HTTP 429 to the deliveries over its ``rate_limit`` per route per
minute (30 unless set) and GitHub does not redeliver. A burst of ``issues.opened`` events (55 issues
filed in a minute) therefore loses triage turns silently. The watchdog calls :func:`sweep`: it lists
the open issues and queues the triage turn of each one the gate would have queued, through the same
``gate.enqueue_isolated`` call ``gate_triage.py`` makes, so the ledger's unique key still stops a
second turn. A noted issue whose triage run later ``succeeded`` is dropped from the
``late-triage.json`` list that ``status`` prints; a failed, cancelled or still-active run stays
noted. That cleanup runs first in every sweep, so it also happens when triage is turned off and when
the open-issues read fails (#1136); it still needs the private runtime's run ledger.

An issue is queued when ``issue_inputs.triage_blocker`` has nothing against it (open, not a pull
request, author in ``triage.authors``, no label from ``triage.labels``), it has no triage row in the
run ledger in any state, it is no newer than ``GRACE_S`` (the gate may still be on its way) and no
older than ``HORIZON_S`` (a backlog from before the loop was armed is not triaged in bulk). An
issue a maintainer already sent to the fixer (``triage.fix_label``) is left alone as well. At most
``PER_SWEEP`` are queued per sweep, oldest first; the rest wait for the next one. The listing is
newest first and bounded (``gh.MAX_ISSUE_PAGES`` pages of 100, pull requests included). It is read
until a page reaches issues older than ``HORIZON_S``, so a window of more than 500 items is still
read whole (#1147); only past the page bound (or when the page past it cannot be read, #1727) does
the sweep work on the newest issues it read and return an alert line saying the older ones were not
checked, with the listing's reason (#1037). A listing the watchdog's budget cuts (``gh_paging``)
reads on from the page it stopped at in the next sweep (``RESUME_FILE``), then starts over from
page 1, so a page the budget never reaches from page 1 is still read (review on #2578). The page
it resumes at is bounded like any later page; when even that does not fit, the next sweep starts
over from page 1 instead of trying it again (F5). It reads on only once every candidate on the
pages it read was queued: one left (past ``PER_SWEEP``, or the ledger or worker down) has the next
sweep read those pages again (F4). A page number is no stable boundary when issues close between
sweeps or requests, so a resumed listing first reads the places around its page's start again
(``triage_resume``): unless they prove it skipped nothing, the next sweep starts over (F6).
"""
from __future__ import annotations

import time

from . import config, gate, gh, triage_resume
from .run_supervisor import issue_inputs
from .util import epoch, log

GRACE_S = 300.0
RESUME_FILE = "triage-sweep-resume.json"    # where a budget-cut listing stopped (#2578)
HORIZON_S = 3 * 86400.0
PER_SWEEP = 10


def candidates(loop: dict, issues: list, queued: set[int], now: float) -> list[int]:
    """The issue numbers, oldest first, whose triage was missed (pure: no GitHub, no ledger)."""
    fix_label = str((loop.get("triage") or {}).get("fix_label") or "").casefold()
    found = []
    for issue in issues:
        number = issue.get("number") if isinstance(issue, dict) else None
        if type(number) is not int or number < 1 or number in queued:
            continue
        if issue_inputs.triage_blocker(loop, issue):
            continue
        if fix_label and fix_label in {str((label or {}).get("name") or "").casefold()
                                       for label in issue.get("labels") or []
                                       if isinstance(label, dict)}:
            continue                      # a maintainer already handed it to the fixer
        created = epoch(issue.get("created_at"))
        if not created or now - created < GRACE_S or now - created > HORIZON_S:
            continue
        found.append(number)
    return sorted(found)


def resolve_late(loop: dict, st) -> bool:
    """Drop the late-triage entries whose triage run has since succeeded (`status` reads them).

    It needs only the ledger and ``late-triage.json``, so :func:`sweep` runs it first: before the
    triage-enabled check (a loop that turned triage off still settles what it noted) and before
    the open-issues read (a GitHub read error does not delay it). Returns True when the run
    ledger was unavailable: it has logged that cause, so the caller does not log it again (#1316).
    """
    try:
        noted = st.late_triage().keys()
        if not noted:
            return False
        try:
            succeeded = gate.isolated_supervisor(loop).seat_numbers(
                loop["repo"], "triage", "succeeded")
        except Exception as exc:          # no private runtime / ledger
            log(f"triage sweep: run ledger unavailable: {type(exc).__name__}: {exc}")
            return True
        st.late_triage_resolve(noted & succeeded)
    except Exception as exc:
        log(f"triage sweep: late-triage cleanup failed: {type(exc).__name__}: {exc}")
    return False


def resume_point(st) -> tuple[int, dict]:
    """The page a budget-cut listing stopped at and what was kept with it (the last item read,
    ``triage_resume.boundary``), or ``(1, {})``: nothing kept, or not a page number."""
    kept = st.kv_get(st.dir / RESUME_FILE, {})
    page = kept.get("next") if isinstance(kept, dict) else None
    ok = isinstance(page, int) and not isinstance(page, bool) and 1 < page <= gh.MAX_ISSUE_PAGES + 1
    return (page, kept) if ok else (1, {})


RESUME_NOTE = {"saved": ", the next sweep reads on from there",
               "held": "; issues it read still wait to be queued, so the next sweep reads "
                       "those pages again",
               "cleared": "; where it stopped could not be saved, so the next sweep starts "
                          "again from page 1",
               "stale": "; where it stopped could not be saved, and an older saved page could "
                        "not be cleared",
               "restart": "; the page it resumed at did not fit the budget, so the next sweep "
                          "starts again from page 1",
               "shifted": "; issues/PRs before where it resumed closed since it last read "
                          "them, so some may have moved onto pages it did not read again: the next "
                          "sweep starts again from page 1"}


def keep_resume(st, record: dict) -> str:
    """Keep where the next sweep starts: the page a budget cut stopped at with the last item read
    (F6), or ``{}``, page 1, for a whole listing (review on #2578). ``saved``; else ``cleared`` when the write failed but the old file
    is gone, so the next sweep starts from page 1; else ``stale``. A failed write is never told
    as a kept one (F3): ``kv_set`` would log and swallow it."""
    path = st.dir / RESUME_FILE
    try:
        st.kv_set_strict(path, record)
        return "saved"
    except Exception as exc:
        log(f"triage sweep: where the listing stopped not saved: {type(exc).__name__}: {exc}")
    try:
        path.unlink(missing_ok=True)
        return "cleared"
    except OSError as exc:
        log(f"triage sweep: old resume point not cleared: {type(exc).__name__}: {exc}")
        return "stale"


def queue_missed(loop: dict, st, issues: list, now: float) -> tuple[list[str], bool]:
    """Queue up to ``PER_SWEEP`` missed triages from ``issues``: the lines, and whether every
    candidate among them was queued (none left past the bound, the ledger or the worker: F4)."""
    lines: list[str] = []
    try:
        queued = gate.isolated_supervisor(loop).seat_numbers(loop["repo"], "triage")
    except Exception as exc:              # no private runtime / ledger: the gate cannot enqueue either
        log(f"triage sweep: run ledger unavailable: {type(exc).__name__}: {exc}")
        return lines, False
    found = candidates(loop, issues, queued, now)
    for number in found[:PER_SWEEP]:
        try:
            outcome = gate.enqueue_isolated(loop, "triage", number, config.TRIAGE_HEAD,
                                            turn_key="triage")
        except Exception as exc:
            lines.append(f"⚠️ Diaktoros [{loop['id']}] issue #{number} triage not queued: "
                         f"isolated worker unavailable: {type(exc).__name__}: {exc} "
                         "— retrying next sweep")
            return lines, False           # the worker is the same for every issue
        log(f"issue #{number} triage {outcome} (recovery sweep)")
        if outcome in ("enqueued", "rearmed", "pending"):
            st.late_triage_note(number, now, outcome)
            lines.append(f"Diaktoros [{loop['id']}] issue #{number} triage queued late: the "
                         "webhook delivery was missed")
    return lines, len(found) <= PER_SWEEP


def sweep(loop: dict, st, now: float | None = None) -> list[str]:
    """Queue the triage turn of each issue whose delivery was missed; return the alert lines."""
    ledger_down = resolve_late(loop, st)
    if not config.triage_enabled(loop):
        return []
    now = time.time() if now is None else now
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - HORIZON_S))
    (start, before), progress = resume_point(st), {}
    # A resumed listing first reads the places around its page's start again, for what moved (F6).
    head, error = triage_resume.overlap(loop, since, start) if start > 1 else ([], "")
    rest, issues = [], head
    if error and head is not None:        # the budget cut those places: none of the page either
        progress["next"] = start
    elif head is not None:
        rest, error = gh.open_issues_read(loop, since, start, progress)
        issues = None if rest is None else triage_resume.joined(head, rest)
    if issues is None:
        log(f"triage sweep: open issues unreadable: {gh.one_line(error)}")
        return []
    # The ledger cleanup just logged why it is down: one line per cause, nothing queued.
    queued, drained = ([], False) if ledger_down else queue_missed(loop, st, issues, now)
    held = "next" in progress and not drained       # these pages again, not the next (F4)
    # A resumed page that never fits starts the next sweep over, not over it again (F5), and so
    # does one not proven to skip nothing since where the last listing stopped (F6).
    cut_back = start > 1 and progress.get("next") == start
    shifted = start > 1 and not cut_back and not triage_resume.proven(before, head, start, rest)
    kept = keep_resume(st, {} if cut_back or shifted else (before if start > 1 else {}) if held
                       else {"next": progress["next"], **triage_resume.boundary(issues)}
                       if "next" in progress else {})
    lines: list[str] = []
    if error:                             # not proven whole: the newest were read, older not
        log(f"triage sweep: open issues incomplete: {gh.one_line(error)}")
        which = (f"the newest {len(issues)}" if start == 1 else
                 f"{len(issues)} (from page {start}, the newer ones last sweep)")
        note = RESUME_NOTE[("restart" if cut_back else "shifted" if shifted else "held" if held
                            else kept) if kept == "saved" else kept]
        lines.append(f"⚠️ Diaktoros [{loop['id']}] triage recovery sweep read only {which} "
                     f"open issues/PRs ({gh.one_line(error)}); older issues inside the sweep "
                     "window were not checked" + (note if "next" in progress else ""))
    elif shifted:
        lines.append(f"⚠️ Diaktoros [{loop['id']}] triage recovery sweep resumed its listing at "
                     f"page {start}" + RESUME_NOTE["shifted" if kept != "stale" else kept])
    if kept == "stale" and "next" not in progress:
        lines.append(f"⚠️ Diaktoros [{loop['id']}] triage recovery sweep could not reset where "
                     "its listing starts: the next sweep may skip the newest open issues")
    return lines + queued
