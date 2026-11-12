#!/usr/bin/env python3
"""Issue #323: ``init`` starts a new loop from the form's turn knobs, and its flags override them."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import re
import shlex
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import config  # noqa: E402
from test_init_concurrency import Base, LOOP_FILE  # noqa: E402

FORM = {"reviewer_max_steps": "120", "fixer_max_steps": "150", "fix_daily_turns": ""}


class InitTurnKnobsTest(Base):
    def test_form_values_reach_the_new_loop(self):
        rc, out = self.init(settings=FORM)
        self.assertEqual(rc, 0, out)
        seats = self.written()["seats"]
        self.assertEqual(seats["reviewer"]["max_steps"], 120)
        self.assertEqual(seats["fixer"]["max_steps"], 150)

    def test_flags_override_the_form(self):
        rc, out = self.init("--reviewer-max-steps", "30", "--fixer-max-steps", "0",
                            "--fix-daily-turns", "0", settings=FORM)
        self.assertEqual(rc, 0, out)
        seats = self.written()["seats"]
        self.assertEqual(seats["reviewer"]["max_steps"], 30)
        self.assertNotIn("max_steps", seats["fixer"])
        self.assertNotIn("issue-fix daily cap", out)

    def test_fix_daily_turns_is_refused_not_dropped(self):
        for args, form in ((("--fix-daily-turns", "7"), None), ((), {"fix_daily_turns": "7"})):
            rc, out = self.init(*args, settings=form)
            self.assertEqual(rc, 2, out)
            self.assertIn("cannot be set by init", out)
            self.assertIn("no triage block", out)       # not the old fix-label rule (#1220)
            self.assertNotIn("no triage.fix_label", out)
            self.assertFalse(LOOP_FILE.exists())

    def test_the_refusals_triage_command_parses(self):
        # #1724: the suggested command left out triage's required --loop, so it exited 2.
        import run_tests
        rc, out = self.init("--fix-daily-turns", "7")
        self.assertEqual(rc, 2, out)
        found = re.search(r"`hermes dk (triage [^`]*)`", out)
        self.assertIsNotNone(found, out)
        # #1728: the same spelling the settings form's refusal names.
        self.assertEqual(found.group(0)[1:-1],
                         config.triage_enable_command(then="--fix-daily-turns N"))
        filled = {"LOOP": "widgets", "P": "triager", "A": "maint", "L": "bug", "N": "9"}
        argv = [filled.get(word, word) for word in shlex.split(found.group(1))]
        args = run_tests.parser_for().parse_args(argv)
        self.assertEqual((args.loop, args.fix_daily_turns), ("widgets", 9))

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
