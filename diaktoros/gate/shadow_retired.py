"""A shadow escalation whose ruling run found the shadow review agreeing (#2688 review F1).

The ruling's launch fails the run when the active reviewer turn's shadow review no longer asks
for changes: there is nothing to rule on, so the escalation's hold must go, or a later dissent
would find it "already" escalated and nothing would rule on it. The run's own finish leaves it
alone: a gate pass between its failure and that clear could replace the marker and re-arm the
run, and the late clear would then drop the replacement (round 5). So every gate pass over the
head (``shadows.shadow_escalation``, from the shadow's finish and the watchdog sweep) drops such
a marker, and only the exact marker it read, while its run is still that failed run, both
checked under the breach lock: the dissent that follows escalates anew and re-arms the run.
"""
from __future__ import annotations

import sqlite3

from .. import gate


def _failed_run(con, repo: str, number: int, head: str, rounds) -> tuple | None:
    """This round's ruling run, as ``(id, updated)``, when it failed finding the review agreeing."""
    from ..run_supervisor.turns import SHADOW_AGREES
    row = con.execute(
        "SELECT id, updated FROM runs WHERE repo=? AND pr=? AND head=? AND seat='adjudicator' "
        "AND turn_key=? AND state='failed' AND error LIKE ?",
        (repo, number, head, f"breach:{rounds}", f"%{SHADOW_AGREES}%")).fetchone()
    return None if row is None else tuple(row)


def _read(db, read):
    """``read(con)`` on the ledger, or None when it cannot be read (the next pass reads again)."""
    from .. import run_supervisor
    try:
        con = run_supervisor.views.read_only_connect(db or run_supervisor.production_ledger())
        if con is None:
            return None
        try:
            return read(con)
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return None


def reconcile(loop: dict, st, number: int, head: str, marker, db=None):
    """The marker as it stands once a retired one is dropped: unchanged unless it is this head's
    shadow escalation, awaiting its ruling, and the ruling run of its round failed finding the
    shadow review agreeing. An unreadable ledger changes nothing (the next pass reads again)."""
    m = marker if isinstance(marker, dict) else {}
    if (m.get("head") != head or m.get("status") != "awaiting-adjudication"
            or (m.get("escalated") or {}).get("by") != gate.SHADOW_ESCALATION):
        return marker
    run = _read(db, lambda con: _failed_run(con, loop["repo"], number, head, m.get("rounds")))
    if run is None:
        return marker

    def still() -> bool:         # under the breach lock: not re-armed (or failed again) since
        return _read(db, lambda con: _failed_run(
            con, loop["repo"], number, head, m.get("rounds"))) == run
    if st.breach_clear_pending(number, head, int(m.get("rounds") or 0), expect=m, still=still):
        return st.breach_get(number)
    return marker
