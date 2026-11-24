"""A shadow escalation whose ruling run found the shadow review agreeing (#2688 review F1).

The ruling's launch fails the run when the active reviewer turn's shadow review no longer asks
for changes: there is nothing to rule on, so the escalation's hold must go. The run's own finish
clears it once the ledger shows it failed, but that clear is skipped while a delivery of the
same marker is in flight, and the delivery then parks it as ``awaiting-adjudication`` on a run
that has already ended. Nothing would then rule on it, and a later dissent would find it
"already" escalated. So every gate pass over the head (``shadows.shadow_escalation``, from the
shadow's finish and the watchdog sweep) checks the ledger again and drops such a marker: the
dissent that follows escalates anew and re-arms the failed run.
"""
from __future__ import annotations

import sqlite3

from .. import gate


def reconcile(loop: dict, st, number: int, head: str, marker, db=None):
    """The marker as it stands once a retired one is dropped: unchanged unless it is this head's
    shadow escalation, awaiting its ruling, and the ruling run of its round failed finding the
    shadow review agreeing. An unreadable ledger changes nothing (the next pass reads again)."""
    from .. import run_supervisor
    from ..run_supervisor.turns import SHADOW_AGREES
    m = marker if isinstance(marker, dict) else {}
    if (m.get("head") != head or m.get("status") != "awaiting-adjudication"
            or (m.get("escalated") or {}).get("by") != gate.SHADOW_ESCALATION):
        return marker
    try:
        con = run_supervisor.views.read_only_connect(db or run_supervisor.production_ledger())
        if con is None:
            return marker
        try:
            retired = con.execute(
                "SELECT 1 FROM runs WHERE repo=? AND pr=? AND head=? AND seat='adjudicator' "
                "AND turn_key=? AND state='failed' AND error LIKE ?",
                (loop["repo"], number, head, f"breach:{m.get('rounds')}",
                 f"%{SHADOW_AGREES}%")).fetchone() is not None
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return marker
    if retired and st.breach_clear_pending(number, head, int(m.get("rounds") or 0)):
        return st.breach_get(number)
    return marker
