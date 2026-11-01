"""Which runs a cancel would stop, and stopping them — one owner for the CLI and the Desktop (#1148).

``hermes dk cancel`` and the Desktop's confirmed cancel both come through this module, so the two
cannot drift apart: one selection rule (``plan``), one state change (``Supervisor.cancel``, called
unchanged), one read-back.

* A run that has not started (pending, blocked, waiting, claimed) is cancelled at once. No
  redelivered event or sweep re-arms it; ``hermes dk retry`` does, on purpose, and a new head
  queues a fresh run as usual.
* A launching or running turn is the owning worker's to stop: the request is recorded, and the
  worker's heartbeat kills the sandbox the way the turn budget does, then ends the run as
  ``cancelled`` (or ``uncertain``, never replayed, when it may already have written).
* A finished run is refused, with its state named.

Nothing here posts to GitHub or reads a token. The CLI's only GitHub read is the PR's head
(``resolve_head``); without one the ledger's newest open head stands.
"""
from __future__ import annotations

import time

from . import config, retry_action
from .run_supervisor import CANCEL_AT_ONCE, CANCEL_BY_WORKER, ISSUE_SEATS, Supervisor

SEATS = retry_action.SEATS
OPEN = CANCEL_AT_ONCE + CANCEL_BY_WORKER
# How long the CLI waits for a worker to stop a running turn (its heartbeat beats every 5s).
STOP_WAIT_S = 30.0
STOP_POLL_S = 0.5
bounded = retry_action.bounded


def _row(con, run_id: str) -> dict | None:
    row = con.execute("SELECT id,repo,pr,seat,head,turn_key,state,error,updated FROM runs "
                      "WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row is not None else None


def plan(con, repo: str, pr: int | None = None, seat: str | None = None,
         run_id: str | None = None, resolve_head=None) -> dict:
    """What a cancel would act on, read from the ledger; nothing is changed.

    ``run_id``: that one run, which must belong to ``repo``. A finished one is listed in
    ``refused`` with its state. ``pr``: the open runs for that number — at the PR's current head
    (``resolve_head()``, else the newest head among its open rows) for the PR seats, every open
    one for the issue seats (keyed by issue number, not a head); ``seat`` narrows it to that
    seat's. An open run at another head is left alone and listed in ``left``: the claim
    supersedes it.
    """
    out = {"head": "", "head7": "", "targets": [], "left": [], "refused": [], "message": ""}
    if run_id is not None:
        row = _row(con, run_id)
        if row is None or row["repo"] != repo:
            out["message"] = f"no run {run_id} in {repo}"
            return out
        out["head"], out["head7"] = str(row["head"] or ""), str(row["head"] or "")[:7]
        if row["state"] in OPEN:
            out["targets"].append(row)
        else:
            out["refused"].append(dict(row, reason=f"run is {row['state']} — it has already finished"))
        return out
    rows = [dict(row) for row in con.execute(
        "SELECT id,repo,pr,seat,head,turn_key,state,error,updated FROM runs WHERE repo=? AND pr=? "
        "ORDER BY created,id", (repo, pr))]
    rows = [row for row in rows if seat is None or row["seat"] == seat]
    open_rows = [row for row in rows if row["state"] in OPEN]
    pr_rows = [row for row in open_rows if row["seat"] not in ISSUE_SEATS]
    head = (resolve_head() if resolve_head is not None else None) or (
        retry_action.newest(pr_rows)["head"] if pr_rows else "")
    out["head"], out["head7"] = str(head or ""), str(head or "")[:7]
    for row in open_rows:
        if row["seat"] in ISSUE_SEATS or row["head"] == head:
            out["targets"].append(row)
        else:
            out["left"].append(dict(row, reason=f"at an older head — the claim supersedes it "
                                                f"(the PR is at {out['head7']})"))
    if not out["targets"]:
        what = f"#{pr}" + (f" ({seat})" if seat else "")
        if not rows:
            out["message"] = f"{what}: no isolated run on record"
        else:
            newest = retry_action.newest(rows)
            out["message"] = (f"{what}: nothing open to cancel — the newest run is {newest['seat']} "
                              f"@ {str(newest['head'])[:7]} {newest['state']}")
    return out


def snapshot(found: dict) -> list:
    """The exact runs a confirmation binds: ids and the states they were seen in."""
    return sorted([row["id"], row["state"]] for row in found["targets"])


def _final(sup: Supervisor, run_ids: list[str], wait: float, sleep=time.sleep) -> dict:
    """Each run's state and error once its worker has stopped it, or as it stands at ``wait``."""
    deadline = time.monotonic() + wait
    while True:
        with sup._connect() as con:
            rows = {run_id: _row(con, run_id) for run_id in run_ids}
        if all(row is None or row["state"] not in CANCEL_BY_WORKER for row in rows.values()) \
                or time.monotonic() >= deadline:
            return rows
        sleep(STOP_POLL_S)


def perform(loop: dict, pr: int | None = None, seat: str | None = None, run_id: str | None = None,
            *, ledger=None, resolve_head=None, expected: list | None = None,
            wait: float = 0.0, reveal_errors: bool = False) -> dict:
    """Cancel what ``plan`` picks, and answer in structured form.

    ``expected`` (the Desktop's confirmation) is the ``snapshot`` the operator was shown: if the
    runs to cancel are no longer exactly those, in those states, nothing is cancelled. ``wait``
    (the CLI's) is how long to wait for a worker to stop a running turn before answering.
    ``reveal_errors`` is the CLI's: its lines may name a run's recorded error, which a page
    never renders.
    """
    if seat is not None and seat not in SEATS:
        raise ValueError(f"seat must be one of {', '.join(SEATS)}")
    if (run_id is None) == (pr is None):
        raise ValueError("name one run (--run) or one PR (--pr)")
    ledger = config.host_path("ledger") if ledger is None else ledger
    out = {"ok": False, "loop": str(loop.get("id") or ""), "pr": pr, "seat": seat or "",
           "run": run_id or "", "head": "", "head7": "", "cancelled": [], "refused": [],
           "left": [], "message": "", "read_at": time.time()}
    if not ledger.exists():
        out["message"] = f"no run ledger at {ledger} — nothing to cancel"
        return out
    sup = Supervisor(ledger)
    with sup._connect() as con:
        found = plan(con, loop["repo"], pr, seat, run_id, resolve_head=resolve_head)
    out["head"], out["head7"] = found["head"], found["head7"]
    out["left"] = [dict(_entry(row, reveal_errors), reason=row["reason"]) for row in found["left"]]
    out["refused"] = [dict(_entry(row, reveal_errors), reason=row["reason"])
                      for row in found["refused"]]
    if found["message"]:
        out["message"] = f"[{loop['id']}] " + found["message"]
        return out
    if expected is not None and snapshot(found) != sorted(list(item) for item in expected):
        out["message"] = (f"[{loop['id']}] #{pr}: refused: the runs changed since you confirmed "
                          "the cancel — nothing was cancelled; preview again")
        return out
    stopping = []
    for row in found["targets"]:
        entry = _entry(row, reveal_errors)
        try:
            done = sup.cancel(row["id"])
        except ValueError as exc:
            out["refused"].append(dict(entry, reason=bounded(exc)))
            continue
        entry["state"] = done["state"]
        out["cancelled"].append(entry)
        if done["state"] == "stopping":
            stopping.append(row["id"])
    if stopping and wait > 0:
        final = _final(sup, stopping, wait)
        for entry in out["cancelled"]:
            row = final.get(entry["id"])
            if row is not None and row["state"] not in CANCEL_BY_WORKER:
                entry["state"] = row["state"]
                if reveal_errors:
                    entry["error"] = row["error"] or ""
    if out["cancelled"]:
        number = pr if pr is not None else found["targets"][0]["pr"]
        # Best effort by its own contract: a ledger that refuses the note never fails the step.
        sup.record_operator_action(loop["repo"], number, "cancel")
    out["ok"] = bool(out["cancelled"]) and not out["refused"]
    return out


def _entry(row: dict, reveal_errors: bool) -> dict:
    entry = {"id": row["id"], "pr": row["pr"], "seat": row["seat"],
             "head7": str(row["head"] or "")[:7], "turn_key": str(row.get("turn_key") or ""),
             "was": row["state"], "state": row["state"]}
    if reveal_errors:
        entry["error"] = row.get("error") or ""
    return entry


def rearm_hint(loop_id: str, entry: dict) -> str:
    """How to re-arm a cancelled run on purpose (only ``retry`` does)."""
    return (f"hermes dk retry --loop {loop_id} --pr {entry['pr']} --seat {entry['seat']}")


def lines(result: dict) -> list[str]:
    """What the CLI prints for a cancel: one line per run, and how to re-arm it."""
    loop_id = result["loop"]
    out = []
    for entry in result["cancelled"]:
        label = (f"[{loop_id}] {entry['seat']} #{entry['pr']} @ {entry['head7']}"
                 + (f" ({entry['turn_key']})" if entry["turn_key"] else ""))
        state = entry["state"]
        if state == "cancelled":
            out.append(f"{label} cancelled (was {entry['was']}); re-arm it on purpose: "
                       f"{rearm_hint(loop_id, entry)}")
        elif state == "stopping":
            out.append(f"{label} stop requested (was {entry['was']}): the worker has not stopped "
                       f"the turn yet; `hermes dk explain --loop {loop_id} --pr {entry['pr']}` "
                       "follows it")
        elif state == "uncertain":
            out.append(f"{label} stopped (was {entry['was']}) but it may have written: "
                       f"{entry.get('error') or 'uncertain'}")
        else:
            out.append(f"{label} was not cancelled: it ended {state} before the stop "
                       f"(was {entry['was']})")
    for entry in result["refused"]:
        out.append(f"[{loop_id}] {entry['seat']} #{entry['pr']} @ {entry['head7']} not cancelled: "
                   f"{entry['reason']}")
    for entry in result["left"]:
        out.append(f"[{loop_id}] {entry['seat']} #{entry['pr']} @ {entry['head7']} {entry['state']} "
                   f"left alone: {entry['reason']}")
    return out

