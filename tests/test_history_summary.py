"""``hermes dk history``'s cycle summary line: totals counted from the same fixture cycle as
``test_history`` (two rounds, five turns, a fixer push, a ruling, a merge), nothing estimated."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
import test_history as th  # noqa: E402
from diaktoros import history_render, history_summary  # noqa: E402


class Summary(fg.Base):
    # The fixture's helpers, borrowed so test_history's own tests do not run twice.
    run_row, insert, cycle = th.History.run_row, th.History.insert, th.History.cycle
    github, collect, run_cli = th.History.github, th.History.collect, th.History.run_cli

    def setUp(self):
        super().setUp()
        self.db = th.run_supervisor.production_ledger()
        self.sup = th.run_supervisor.Supervisor(self.db)
        self.reads = []

    def test_a_merged_cycle_s_totals(self):
        self.cycle()
        data = self.collect()
        s = data["summary"]
        self.assertEqual((s["rounds"], s["turns"]), (2, 5))
        self.assertEqual(s["seats"], {"adjudicator": 1, "fixer": 1, "reviewer": 2, "shadow": 1})
        self.assertEqual((s["tokens"], s["tokens_unrecorded"]), (410_000, 4))
        self.assertEqual((s["cost"], s["cost_unrecorded"]), (None, 5))
        self.assertEqual(s["first_review_at"], th.T + 30)
        self.assertEqual(s["review_to_merge_s"], 570)          # first review T+30, merged T+600
        self.assertEqual(s["findings"], {"fixed": 1, "overruled": 1})
        want = ("2 rounds · 5 turns · 410k tokens (4 turn(s) unrecorded) · first review to "
                "merge 9m · findings: 2 raised, 1 fixed, 1 overruled")
        self.assertIn(f"summary: {want}\n", history_render.text(data))
        self.assertIn(f"**Summary:** {want}\n", history_render.markdown(data))

    def test_the_summary_is_in_the_json(self):
        self.cycle()
        rc, out = self.run_cli("--format", "json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["schema"], 1)                    # an added key keeps the schema
        self.assertEqual(data["summary"]["turns"], 5)

    def test_what_is_not_on_record_is_not_counted(self):
        """Offline: no merge read, so no review-to-merge span; no turn recorded tokens."""
        self.run_row("reviewer", th.A, th.T)
        data = self.collect(offline=True)
        s = data["summary"]
        self.assertIsNone(s["review_to_merge_s"])
        self.assertIsNone(s["tokens"])
        line = history_summary.line(data)
        self.assertEqual(line, "1 round · 1 turn · tokens not recorded")

    def test_cost_and_long_spans_read_plainly(self):
        data = {"summary": {"rounds": 3, "turns": 2, "seats": {}, "tokens": 2_500_000,
                            "tokens_unrecorded": 0, "cost": 1.5, "cost_unrecorded": 1,
                            "first_review_at": 1.0, "review_to_merge_s": 3 * 86400 + 7200,
                            "findings": {}}}
        self.assertEqual(history_summary.line(data), "3 rounds · 2 turns · 2.5M tokens · $1.50 "
                         "(partial) · first review to merge 3d 2h")
        data["summary"]["review_to_merge_s"] = 5400
        self.assertIn("first review to merge 1h 30m", history_summary.line(data))
        self.assertEqual(history_summary.line({}), "")


if __name__ == "__main__":
    unittest.main()
