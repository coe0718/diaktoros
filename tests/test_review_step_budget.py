"""#2199: a review's prompt names the seat's own step budget and when to submit by.

Shadow turns settled a verdict and ran out of steps fixing a refused body: the model was never
told its budget. The shadow is prompted as the reviewer, so each seat's value is checked through
the real launch (``_prepare_launch``), not only the prompt builder."""
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
import test_fixer_gating as fg  # noqa: E402
from diaktoros import prompts, run_supervisor  # noqa: E402


class StepBudget(fg.Base):
    def setUp(self):
        super().setUp()
        self.loop = {**self.loop, "shadow": {"profile": "critic", "every": 1},
                     "seats": {**(self.loop.get("seats") or {}),
                               "reviewer": {**((self.loop.get("seats") or {}).get("reviewer") or {}),
                                            "max_steps": 37},
                               "shadow": {"max_steps": 73}}}
        self.db = run_supervisor.production_ledger()

    def launch(self, seat):
        from diaktoros import ci, gh, issue_facts
        from diaktoros.run_supervisor import pr_facts
        sup = run_supervisor.Supervisor(self.db)
        delivery = f"{fg.REPO}:7:{fg.HEAD}:{seat}:t"
        sup.enqueue(delivery, fg.REPO, 7, fg.HEAD, seat, turn_key="t")
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state='running', owner='w', generation='g', "
                        "launch_intent=? WHERE seat=?", (time.time(), seat))
        row = sup.get(delivery)
        turn = run_supervisor._Turn(row["id"], "w", 60, row=row, loop=self.loop)
        pr = {**fg.LIVE, "head": {"sha": fg.HEAD, "ref": "fix-7"}}
        change = pr_facts.PRChange("## The change under review", "")
        with mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=[]), \
                mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
                mock.patch.object(issue_facts, "section", return_value=""), \
                mock.patch.object(ci, "read", return_value=None), \
                mock.patch.object(ci, "section", return_value=""), \
                mock.patch.object(pr_facts, "pr_change", return_value=change), \
                mock.patch.object(pr_facts, "pr_record", return_value="RECORD"):
            return sup._prepare_launch(turn).prompt

    def test_each_review_seat_is_told_its_own_budget(self):
        for seat, steps in (("reviewer", 37), (run_supervisor.SHADOW, 73)):
            with self.subTest(seat):
                text = self.launch(seat)
                self.assertIn(f"This turn has {steps} agent steps", text)
                self.assertIn(f"Submit by step {steps - prompts.STEP_RESERVE} at the latest", text)

    def test_a_small_budget_still_leaves_half_for_the_work(self):
        self.assertIn("Submit by step 4 ", prompts.step_budget(8))
        self.assertIn("Submit by step 90 ", prompts.step_budget(100))


if __name__ == "__main__":
    unittest.main()
