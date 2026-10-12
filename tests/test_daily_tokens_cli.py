#!/usr/bin/env python3
"""#480: the daily token budget is reachable from init, setup, set and apply (CLI level)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from diaktoros import cli, config  # noqa: E402
from test_init_concurrency import Base, LOOP_FILE, LOOP_ID  # noqa: E402


class DailyTokensCli(Base):
    def budget(self, seat):
        return self.written()["seats"][seat].get("daily_tokens")

    def test_init_flags_and_form(self):
        rc, out = self.init("--reviewer-daily-tokens", "5000", "--fixer-daily-tokens", "7000")
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.budget("reviewer"), self.budget("fixer")), (5000, 7000))
        LOOP_FILE.unlink()
        rc, out = self.init(settings={"reviewer_daily_tokens": "123", "fixer_daily_tokens": "456"})
        self.assertEqual(rc, 0, out)
        self.assertEqual((self.budget("reviewer"), self.budget("fixer")), (123, 456))
        LOOP_FILE.unlink()
        rc, out = self.init("--fixer-daily-tokens", "0",
                            settings={"fixer_daily_tokens": "456"})   # flag wins; 0 = none
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.budget("fixer"))

    def test_init_refuses_a_bad_value_and_writes_nothing(self):
        rc, out = self.init("--fixer-daily-tokens", "-5")
        self.assertEqual(rc, 2, out)
        self.assertIn("daily_tokens", out)
        self.assertFalse(LOOP_FILE.exists())

    def test_set_sets_and_removes(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--reviewer-daily-tokens", "900")
        self.assertEqual(rc, 0, out)
        self.assertIn("reviewer daily tokens", out)
        self.assertEqual(self.budget("reviewer"), 900)
        self.assertEqual(config.seat_daily_tokens(config.load_id(LOOP_ID), "reviewer"), 900)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--fixer-daily-tokens", "800")
        self.assertEqual((self.budget("reviewer"), self.budget("fixer")), (900, 800))
        rc, out = self.cli("set", "--loop", LOOP_ID, "--reviewer-daily-tokens", "0")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self.budget("reviewer"))
        self.assertEqual(self.budget("fixer"), 800)

    def test_set_refuses_out_of_range_and_writes_nothing(self):
        self.assertEqual(self.init()[0], 0)
        before = LOOP_FILE.read_bytes()
        rc, out = self.cli("set", "--loop", LOOP_ID, "--fixer-daily-tokens",
                           str(config.DAILY_TOKENS_MAX + 1))
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(LOOP_FILE.read_bytes(), before)

    def test_apply_saves_then_matches_and_refuses_bad(self):
        self.assertEqual(self.init()[0], 0)
        for key, seat in (("reviewer_daily_tokens", "reviewer"), ("fixer_daily_tokens", "fixer")):
            rc, out = self.cli("apply", "--loop", LOOP_ID, settings={key: "4242"})
            self.assertEqual(rc, 0, out)
            self.assertIn("daily tokens", out)
            self.assertEqual(self.budget(seat), 4242)
            rc, out = self.cli("apply", "--loop", LOOP_ID, settings={key: "4242"})
            self.assertIn("already matches", out)
        before = self.written()
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"fixer_daily_tokens": "abc"})
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.written(), before)

    def test_setup_hands_init_the_budget(self):
        args = t.parser_for({}).parse_args(
            ["setup", "--repo", t.REPO, "--yes", "--reviewer-daily-tokens", "11",
             "--fixer-daily-tokens", "22"])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--reviewer-daily-tokens=11", argv)
        self.assertIn("--fixer-daily-tokens=22", argv)

    def test_status_shows_the_spend(self):
        self.assertEqual(self.init("--fixer-daily-tokens", "1000")[0], 0)
        rc, out = self.cli("status", "--loop", LOOP_ID)
        self.assertIn("fixer 0/1,000 tokens today", out)


if __name__ == "__main__":
    unittest.main()
