#!/usr/bin/env python3
"""Issue #323: ``init`` starts a new loop from the form's turn knobs, and its flags override them."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_init_concurrency import Base, LOOP_FILE  # noqa: E402

FORM = {"reviewer_max_steps": "120", "fixer_max_steps": "150", "fix_daily_turns": "7"}


class InitTurnKnobsTest(Base):
    def test_form_values_reach_the_new_loop(self):
        rc, out = self.init(settings=FORM)
        self.assertEqual(rc, 0, out)
        seats = self.written()["seats"]
        self.assertEqual(seats["reviewer"]["max_steps"], 120)
        self.assertEqual(seats["fixer"]["max_steps"], 150)
        # A new loop has no triage.fix_label to hold the cap; init says so instead of dropping it.
        self.assertIn("issue-fix daily cap 7 is not written", out)

    def test_flags_override_the_form(self):
        rc, out = self.init("--reviewer-max-steps", "30", "--fixer-max-steps", "0",
                            "--fix-daily-turns", "0", settings=FORM)
        self.assertEqual(rc, 0, out)
        seats = self.written()["seats"]
        self.assertEqual(seats["reviewer"]["max_steps"], 30)
        self.assertNotIn("max_steps", seats["fixer"])
        self.assertNotIn("issue-fix daily cap", out)

    def test_blank_form_leaves_the_defaults(self):
        rc, out = self.init(settings={"reviewer_max_steps": "", "fixer_max_steps": "",
                                      "fix_daily_turns": ""})
        self.assertEqual(rc, 0, out)
        seats = self.written()["seats"]
        for seat in ("reviewer", "fixer"):
            self.assertNotIn("max_steps", seats[seat])
        self.assertNotIn("issue-fix daily cap", out)

    def test_out_of_range_is_refused_and_writes_nothing(self):
        rc, out = self.init("--fixer-max-steps", "3")
        self.assertEqual(rc, 2, out)
        self.assertIn("seats.fixer.max_steps", out)
        self.assertFalse(LOOP_FILE.exists())


if __name__ == "__main__":
    unittest.main()
