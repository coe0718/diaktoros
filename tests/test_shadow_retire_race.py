"""#2688 review F1, round 5: dropping an agreeing shadow escalation never drops its replacement.

A ruling run that finds the shadow review agreeing fails, and the next gate pass drops its
escalation (``gate.shadow_retired``) so a later dissent escalates anew and re-arms the run. Two
late clears could drop that new escalation, leaving the re-armed run to find no marker and be
superseded: the run's own finish clearing after a gate pass that ran between its failure and
that clear, and a gate pass whose ledger read came before another pass's re-escalation. The
finish no longer clears, and a pass clears only the exact marker it read while its run is
still that failed run. Real worker finish, real breach delivery and real ledger enqueue.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_shadow_escalate_ruling as ser  # noqa: E402
import test_shadow_escalate_superseded as ses  # noqa: E402
from diaktoros.run_supervisor import waits  # noqa: E402

S = ses.Superseded


class Race(ser.Base):
    claim, escalate = ser.Between.claim, ser.Trigger.escalate
    reviewer, superseded, own_review = S.reviewer, S.superseded, S.own_review
    run_turn, claim_id, adjudicator_row = S.run_turn, S.claim_id, S.adjudicator_row
    escalate_for_real = S.escalate_for_real

    def agreeing(self):
        self.superseded()
        self.own_review("APPROVE", "agrees now")

    def dissent(self):
        self.reviewer("reconsider:2", time.time())
        self.own_review("REQUEST_CHANGES", "F3: a later dissent", key="reconsider:2")

    def test_a_gate_pass_between_the_runs_failure_and_its_finish_keeps_the_new_escalation(self):
        self.agreeing()
        note, seen = waits.note, {}

        def gate_pass(sup, run_id):     # the ledger already shows the run failed; finish goes on
            if not seen and self.adjudicator_row()["state"] == "failed":
                self.dissent()
                seen["outcome"] = self.escalate_for_real()
                seen["marker"], seen["state"] = self.st.breach_get(7), self.adjudicator_row()["state"]
            return note(sup, run_id)
        with mock.patch.object(waits, "note", side_effect=gate_pass):
            self.run_turn(lambda: None)
        self.assertEqual((seen["outcome"], seen["state"]), ("new", "pending"))   # re-armed
        self.assertEqual(seen["marker"]["status"], "awaiting-adjudication")
        self.assertEqual(self.st.breach_get(7), seen["marker"])   # the finish left the new one
        self.assertEqual(self.adjudicator_row()["state"], "pending")

    def stale_pass(self, between):
        """A gate pass that has read the failed run, with ``between()`` run before its clear."""
        clear, calls = self.st.breach_clear_pending, []

        def late(*args, **kwargs):
            calls.append(kwargs.get("expect"))
            if len(calls) == 1:
                between()
            return clear(*args, **kwargs)
        with mock.patch.object(self.st, "breach_clear_pending", side_effect=late):
            return self.escalate_for_real(), calls

    def test_a_pass_that_read_the_failed_run_before_a_re_escalation_drops_nothing(self):
        self.agreeing()
        self.run_turn(lambda: None)
        self.assertEqual(self.adjudicator_row()["state"], "failed")
        old = self.st.breach_get(7)
        self.assertEqual(old["status"], "awaiting-adjudication")   # the finish left it to the gate
        self.dissent()
        inner = {}

        def other_pass():               # drops the old marker, escalates anew, re-arms the run
            inner["outcome"] = self.escalate_for_real()
            inner["marker"] = self.st.breach_get(7)
        outcome, calls = self.stale_pass(other_pass)
        self.assertEqual(calls[0], old)
        self.assertEqual(inner["outcome"], "new")
        self.assertEqual(outcome, "already")
        self.assertNotEqual(inner["marker"], old)
        self.assertEqual(self.st.breach_get(7), inner["marker"])   # the replacement stands
        self.assertEqual(self.adjudicator_row()["state"], "pending")

    def test_a_pass_that_read_the_failed_run_before_it_was_re_armed_drops_nothing(self):
        # Same marker, but the run is no longer that failed run: its generation decides.
        self.agreeing()
        self.run_turn(lambda: None)
        old = self.st.breach_get(7)

        def rearm():
            with closing(sqlite3.connect(self.db)) as con, con:
                con.execute("UPDATE runs SET state='pending', error=NULL, updated=updated+1 "
                            "WHERE seat='adjudicator'")
        _outcome, calls = self.stale_pass(rearm)
        self.assertEqual(calls, [old])
        self.assertEqual(self.st.breach_get(7), old)
        self.assertEqual(self.adjudicator_row()["state"], "pending")


if __name__ == "__main__":
    unittest.main()
