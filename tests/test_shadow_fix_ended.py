"""#2263: what ``ready`` and ``explain`` say once an upheld shadow review's fixer turn
(``shadow:<ruling time>``) has ended at the head with no push.

``explain`` already names such a turn (#2262). ``ready``'s merge check still skipped the
escalation's "a person decides" line on ``shadow_fix_owed`` alone, which never reads that turn,
so it kept promising the fixer's answer (#2263).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
