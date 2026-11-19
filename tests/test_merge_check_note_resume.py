"""The merge check's turn note keeps its resume time, and a changed hold refreshes the check.

#2325: a held line longer than ``views.TURN_NOTE_MAX`` lost its "— resumes …" end to the note's
bound before ``merge_check.description`` could keep the tail. #2326: a changed CI hold reason
written by the claim's light poll, and the claim's two terminal writes (a refused fixer row, an
unavailable review generation), never refreshed the merge check, so it showed the old reason until
the watchdog's sweep.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_turn_in_merge_check as tim  # noqa: E402
from diaktoros import merge_check, pacing  # noqa: E402
from diaktoros.run_supervisor import CI_HOLD, views  # noqa: E402

UNTIL = 1794000000.0


class HeldNote(tim.Ledger, unittest.TestCase):
    def setUp(self):
        self.make_ledger()

    def held(self, reason):
        line = pacing.hold_line("reviewer", "openai-codex", UNTIL, reason)
        self.add("reviewer", "waiting", line)
        return self.note(), f"— resumes {pacing.when(UNTIL)}"

    def test_a_provider_unavailable_hold_keeps_its_resume_time(self):
        # The issue's own line: 99 characters, cut to "… — resumes 2026-1…" before #2325.
        note, resumes = self.held("provider unavailable (empty stream with no finish_reason)")
        self.assertEqual(note, "reviewer turn held: provider unavailable (empty stream with no "
                               f"finish_reason) {resumes}")

    def test_a_longer_reason_is_cut_before_the_resume_time(self):
        note, resumes = self.held("provider unavailable (" + "upstream said no; " * 8 + ")")
        self.assertTrue(note.endswith(resumes), note)
        self.assertIn("…", note)
        self.assertTrue(note.startswith("reviewer turn held: provider unavailable (upstream"))
        self.assertLessEqual(len(note), len("reviewer turn held: ") + views.TURN_NOTE_MAX)
        # and the commit status's one line keeps it too
        said = merge_check.description(merge_check.PENDING,
                                       f"not approved at head {tim.HEAD[:7]} — {note}")
        self.assertTrue(said.endswith(resumes), said)

    def test_a_long_held_line_without_a_resume_time_is_bounded_as_before(self):
        self.add("reviewer", "waiting", f"{CI_HOLD} on {tim.HEAD[:7]} — " + "check, " * 30)
        note = self.note()
        self.assertTrue(note.startswith("reviewer turn held: waiting for CI on aaaaaaa — "), note)
        self.assertLessEqual(len(note), len("reviewer turn held: ") + views.TURN_NOTE_MAX)
        self.assertTrue(note.endswith("…"), note)


if __name__ == "__main__":
    unittest.main()
