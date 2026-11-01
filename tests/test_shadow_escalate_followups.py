"""Follow-ups to #1004 part 2, from the shadow review of #1103.

#1114: every shadow escalation is at an approved head, and the sweep stopped at an approved head
before its stall scan, so a ruling turn that died was never reported (while the hold kept saying
"waiting for its ruling"). A re-armed marker (#1111) is ended only by its own round's ruling.
#1115: ``state.breach_reopen`` re-arms one marker, once.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
import test_shadow_escalate_ruling as ser  # noqa: E402
from diaktoros import gate  # noqa: E402
from diaktoros import watch_mode  # noqa: E402


class Stall(ser.Base):
    def sweep(self):
        from diaktoros import gh, pr_hold
        from scripts import watchdog
        with mock.patch.object(watch_mode, "TEST", True), \
             mock.patch.object(gate, "shadow_escalation", return_value="already"), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=[fg.LIVE]), \
             mock.patch.object(gh, "pr", return_value=fg.LIVE), \
             mock.patch.object(gh, "reviews", return_value=list(ser.APPROVED)), \
             mock.patch.object(pr_hold, "read", return_value=(None, "")), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire"), \
             mock.patch.object(watchdog.observer, "notify"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"):
            watchdog.sweep_loop(self.loop, self.st)          # first sweep: snapshot only
            return "\n".join(watchdog.sweep_loop(self.loop, self.st))

    def test_a_dead_ruling_turn_at_an_approved_head_is_a_stall(self):
        self.mark_escalated(status="adjudicating")   # no adjudicator run on record
        self.assertIn("the ruling is not coming by itself", self.sweep())

    def test_a_ruling_or_another_escalation_is_not(self):
        self.mark_escalated(status="adjudicating")
        self.ruled("ACCEPT")                         # turn key breach:1, the marker's round
        self.assertNotIn("not coming", self.sweep())
        self.mark_escalated(by="operator", status="adjudicating")   # approval ends it
        self.assertNotIn("not coming", self.sweep())

    def test_a_pending_delivery_is_left_to_its_retry(self):
        self.mark_escalated(status="delivery-pending")
        self.assertNotIn("parked awaiting", self.sweep())

    def test_only_the_rearmed_rounds_ruling_ends_it(self):
        marker = self.mark_escalated(status="adjudicating")
        self.ruled("ACCEPT")                         # round 1's
        marker["rounds"] = 2                          # re-armed for a later dissent, then died
        from diaktoros import state as state_mod
        state_mod._atomic_write(self.st.breach, {f"{fg.REPO}#7": marker})
        self.assertIn("the ruling is not coming by itself", self.sweep())


class Reopen(ser.Base):
    def test_one_reopen_per_round(self):
        self.mark_escalated(status="adjudicating")
        self.assertTrue(self.st.breach_reopen(7, fg.HEAD, 1, "again"))
        self.assertFalse(self.st.breach_reopen(7, fg.HEAD, 1, "again"))   # a racing second
        marker = self.st.breach_get(7)
        self.assertEqual((marker["rounds"], marker["status"]), (2, "delivery-pending"))

    def test_only_this_head_round_and_a_ruling_out(self):
        for status, head, rounds in (("awaiting-adjudication", fg.HEAD, 1),
                                     ("delivery-pending", fg.HEAD, 1),
                                     ("adjudicating", "b" * 40, 1),
                                     ("adjudicating", fg.HEAD, 2)):
            with self.subTest(status=status, head=head[:1], rounds=rounds):
                self.mark_escalated(status=status)
                self.assertFalse(self.st.breach_reopen(7, head, rounds, "again"))
                self.assertEqual(self.st.breach_get(7)["rounds"], 1)

    def test_racing_dissents_wake_one_adjudicator(self):
        from diaktoros import gh
        self.shadow("REQUEST_CHANGES")
        self.mark_escalated(status="adjudicating")
        self.ruled("ACCEPT")
        self.shadow("REQUEST_CHANGES")
        with mock.patch.object(gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gate.observer, "notify"), \
                mock.patch.object(gate, "wake_adjudicator", return_value=True) as wake:
            stale = self.st.breach_get(7)
            first = gate.shadow_escalation(self.loop, self.st, 7, fg.HEAD, ser.APPROVED, db=self.db)
            # The second caller read the marker before the first re-armed it.
            with mock.patch.object(self.st, "breach_get", return_value=stale):
                second = gate.shadow_escalation(self.loop, self.st, 7, fg.HEAD, ser.APPROVED,
                                                db=self.db)
        self.assertEqual((first, second), ("new", "already"))
        wake.assert_called_once()
        self.assertEqual(self.st.breach_get(7)["rounds"], 2)


if __name__ == "__main__":
    unittest.main()
