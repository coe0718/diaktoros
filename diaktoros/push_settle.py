"""Settle a fixer push quarantine that wrote nothing (#1763, part 2).

A fixer push held as "may have written" keeps the fixer seat until an operator reconciles it.
Part 1 (#1792) stops a push that fails before its attempt from being held at all; this settles
one already held whose branch never moved: one recorded before that fix, or a worker that died
between its write-ahead intent and its attempt ("unresolved push intent").

The watchdog sweep, with a complete open-PR listing, settles such a run when every one of these
holds, and leaves it to the operator otherwise:

* its worker is positively gone, checked before anything else is read, so it cannot write
  after (a missing PID is not gone), and again when the run is settled;
* the run is still an ``uncertain`` fixer push quarantine the host never saw publish
  (``Supervisor.settle_push_quarantine`` checks the ledger side under its lock);
* its PR is open and still at the run's head (the listing);
* its branch ref, read now, is still at the run's head;
* the broker audit has no push attempt for that PR and head since the run began. The audit is
  written and fsynced before the push (``safe_push.before_push``), so a push with no attempt
  never reached GitHub.

An unknown answer is never a clean one: an unreadable ref, audit or listing entry leaves the run
held. A settled run is ``failed`` with the evidence as its error, so `retry` or a redelivered
event re-arms it, unlike an operator reconciliation, which never is.
"""
from __future__ import annotations

import json
from urllib.parse import quote

from . import config, gh
from .util import finite_time


def sweep(loop: dict, prs: list) -> list[str]:
    """Settle this loop's provable push quarantines, given a complete open-PR listing; the
    operator lines for what was settled. Best effort: anything unknown stays held."""
    import sqlite3
    from .run_supervisor import PUSH_QUARANTINE, Supervisor, presence_marker, views
    ledger = config.host_path("ledger")
    con = views.read_only_connect(ledger) if ledger.exists() else None
    if con is None:
        return []
    try:
        con.row_factory = sqlite3.Row
        held = con.execute("SELECT id,pr,head,created FROM runs WHERE repo=? AND seat='fixer' "
                           "AND state='uncertain' AND error LIKE ?",
                           (loop["repo"], PUSH_QUARANTINE + "%")).fetchall()
    finally:
        con.close()
    if not held:
        return []
    sup = Supervisor(ledger, presence=presence_marker())
    open_prs = {pr.get("number"): pr for pr in prs if isinstance(pr, dict)}
    lines = []
    for run in held:
        # The worker first, before any proof is read: a worker still running could write its
        # attempt and push after the reads (review of #1802, F1). The settle checks it again.
        if not sup.push_worker_gone(run["id"]):
            continue
        evidence = _nothing_written(loop, open_prs.get(run["pr"]), run)
        if evidence and sup.settle_push_quarantine(run["id"], evidence=evidence):
            lines.append(f"Diaktoros [{loop['id']}] #{run['pr']}: a fixer push held as "
                         f"may-have-written at {run['head'][:7]} is settled: {evidence}. "
                         "`hermes dk retry` (or the next event) re-arms it.")
    return lines


def _nothing_written(loop: dict, pr: dict | None, run) -> str:
    """The evidence that ``run``'s push never reached GitHub, or ``""`` when it cannot be shown."""
    head = run["head"]
    if not isinstance(pr, dict) or (pr.get("head") or {}).get("sha") != head:
        return ""                                   # closed, moved or unlisted: the operator's
    branch = (pr.get("head") or {}).get("ref")
    if not isinstance(branch, str) or not branch:
        return ""
    ref = gh.api(loop, f"/repos/{loop['repo']}/git/ref/heads/{quote(branch, safe='/')}",
                 login=loop.get("read_token"))
    if (not isinstance(ref, dict) or ref.get("ref") != f"refs/heads/{branch}"
            or (ref.get("object") or {}).get("sha") != head):
        return ""
    if _attempted(loop, run) is not False:
        return ""
    return (f"branch {branch} and PR #{run['pr']} still at {head[:7]}, no push attempt in the "
            "broker audit, no live worker")


def _attempted(loop: dict, run) -> bool | None:
    """Whether the broker audit records a push attempt for this PR and head since the run began;
    None when the audit cannot be read whole (an unknown is never "no attempt")."""
    audit = config.state_dir(loop) / "broker-audit.jsonl"
    try:
        text = audit.read_text()
    except FileNotFoundError:
        return False                                # no audit was ever written: no attempt
    except (OSError, UnicodeDecodeError):
        return None
    since = float(run["created"] or 0)
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            return None                             # a line it cannot read may be the attempt
        if not isinstance(record, dict):
            return None                             # nor one it cannot read as a record (F3)
        if (record.get("phase") == "attempt" and record.get("repo") == loop["repo"]
                and record.get("pr") == run["pr"] and record.get("old_head") == run["head"]):
            at = record.get("at")
            at = finite_time(at)
            if at is None:
                # An attempt with no time, or one no comparison can place (NaN, an overflow to
                # infinity: review of #1802, F4; an integer too large for a float: #1828):
                # maybe this run's.
                return None
            if at >= since:
                return True
    return False
