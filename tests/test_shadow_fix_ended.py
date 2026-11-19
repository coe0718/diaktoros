"""#2263, #2541: what ``ready`` and ``explain`` say once an upheld shadow review's fixer turn
(``shadow:<ruling time>``) has ended at the head with no push.

``explain`` already names such a turn (#2262). ``ready``'s merge check still skipped the
escalation's "a person decides" line on ``shadow_fix_owed`` alone, which never reads that turn,
so it kept promising the fixer's answer (#2263). And where ``retry`` re-arms this ruling's turn
together with an older ruling's obsolete one, ``explain`` named only the current turn (#2541).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
import test_shadow_escalate_ruling as ruling  # noqa: E402
import test_shadow_upheld_fix as upheld  # noqa: E402
from diaktoros import run_supervisor  # noqa: E402


class Ended(ruling.Base):
    # The #1353 helpers, borrowed (not inherited, so their tests do not run twice).
    upheld_marker = upheld.Operator.upheld_marker
    ready = upheld.Operator.ready
    explain = upheld.Operator.explain
    shadow_fix_row = upheld.Operator.shadow_fix_row

    def setUp(self):
        super().setUp()
        self.upheld_marker()
        self.loop = {**self.loop, "unattended_fixer_push": True}

    def clear(self):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("DELETE FROM runs WHERE seat='fixer'")

    def test_ready_names_a_shadow_fix_turn_that_ended_without_a_push(self):
        # #2263: one the sweep re-arms (a pre-write failure), or one still queued or running,
        # still answers the findings; one that ended otherwise leaves them to a person.
        for state, error, ended in (
                ("pending", "", False),
                ("running", "", False),
                ("failed", "", False),                          # the sweep re-arms it
                ("succeeded", "", True),
                ("cancelled", run_supervisor.OPERATOR_CANCEL, True),
                ("uncertain", "", True)):
            with self.subTest(state=state):
                self.shadow_fix_row(state, error)
                why = self.ready()
                stuck = [w for w in why if w.startswith("shadow fix:")]
                self.assertEqual(len(stuck), int(ended), why)
                self.assertFalse(any(w.startswith("escalated:") for w in why), why)
                if ended:
                    self.assertIn(f"at head {fg.HEAD[:7]} is {state} with no fix pushed", stuck[0])
                    self.assertIn("a person decides", stuck[0])
                else:
                    self.assertFalse(any("a person decides" in w for w in why), why)
                # explain reads the same turn the same way (#2262).
                report = self.explain()
                self.assertEqual(any(b.startswith("shadow fix:") for b in report["blockers"]),
                                 ended, report["blockers"])
                self.clear()

    def test_ready_still_promises_the_fixer_before_its_turn_is_in_the_ledger(self):
        why = self.ready()
        self.assertTrue(any("the fixer answers its findings" in w for w in why), why)
        self.assertFalse(any(w.startswith(("shadow fix:", "escalated:")) for w in why), why)

    def test_explain_names_the_obsolete_turn_retry_also_rearms(self):
        # #2541: this ruling's turn and an older ruling's, both cancelled by an operator; retry
        # re-arms both, and the step said only that it re-arms this one.
        retry = f"hermes dk retry --loop {self.loop['id']} --pr 7 --seat fixer"
        old = f"{run_supervisor.SHADOW_FIX_KEY}1"
        sup = self.shadow_fix_row("cancelled", run_supervisor.OPERATOR_CANCEL)
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue(f"{fg.REPO}:7:{fg.HEAD}:fixer:{old}", fg.REPO, 7, fg.HEAD, "fixer",
                        turn_key=old)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state='cancelled', error=?, push_admitted=1 "
                        "WHERE turn_key=?", (run_supervisor.OPERATOR_CANCEL, old))
        report = self.explain()
        self.assertEqual(report["next"]["kind"], "fixer-retry", report["next"])
        action = report["next"]["action"]
        self.assertIn(f"`{retry}` re-arms it, and also re-arms {old} (cancelled), which these "
                      "findings do not need", action)

    def test_explain_names_no_other_turn_when_retry_rearms_only_this_one(self):
        self.shadow_fix_row("cancelled", run_supervisor.OPERATOR_CANCEL)
        report = self.explain()
        self.assertEqual(report["next"]["kind"], "fixer-retry", report["next"])
        self.assertNotIn("also re-arms", report["next"]["action"])


if __name__ == "__main__":
    unittest.main()
