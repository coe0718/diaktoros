#!/usr/bin/env python3
"""#1163 M2b: ``merge_check`` (auto, on, off) is reachable from init, setup, set, apply and show."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from diaktoros import config  # noqa: E402
from diaktoros.cli import install as cli_install  # noqa: E402
from test_init_concurrency import Base, LOOP_FILE, LOOP_ID  # noqa: E402


class MergeCheckCli(Base):
    def mode(self):
        return self.written().get("merge_check")

    def test_init_defaults_to_auto_and_takes_the_flag_or_the_form(self):
        self.assertEqual(self.init()[0], 0)
        self.assertEqual(self.mode(), "auto")
        LOOP_FILE.unlink()
        rc, out = self.init("--merge-check", "on")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.mode(), "on")
        LOOP_FILE.unlink()
        rc, out = self.init(settings={"merge_check": "OFF"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.mode(), "off")
        LOOP_FILE.unlink()
        rc, out = self.init("--merge-check", "on", settings={"merge_check": "off"})  # flag wins
        self.assertEqual((rc, self.mode()), (0, "on"), out)

    def test_set_changes_it_and_a_bad_value_writes_nothing(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--merge-check", "on")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.mode(), "on")
        self.assertTrue(config.merge_check_on(config.load_id(LOOP_ID)))
        before = LOOP_FILE.read_bytes()
        with self.assertRaises(SystemExit):
            self.cli("set", "--loop", LOOP_ID, "--merge-check", "sometimes")
        self.assertEqual(LOOP_FILE.read_bytes(), before)

    def test_apply_saves_then_matches_and_refuses_bad(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"merge_check": "on"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.mode(), "on")
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"merge_check": "on"})
        self.assertIn("already matches", out)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"merge_check": ""})  # blank: unset here
        self.assertEqual(self.mode(), "on")
        before = self.written()
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"merge_check": "maybe"})
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.written(), before)

    def test_setup_hands_init_the_mode(self):
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--yes",
                                            "--merge-check", "off"])
        argv, _ = cli_install._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--merge-check=off", argv)

    def test_show_says_what_auto_means_here(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("show", "--loop", LOOP_ID)
        self.assertEqual(rc, 0, out)
        self.assertIn("merge_check", out)
        self.assertIn("off — the reviewer uses a PAT: `on` posts a commit status", out)
        self.assertEqual(self.cli("set", "--loop", LOOP_ID, "--merge-check", "on")[0], 0)
        rc, out = self.cli("show", "--loop", LOOP_ID)
        self.assertIn("a commit status from the reviewer's PAT", out)
        self.assertNotIn("off — the reviewer uses a PAT", out)


if __name__ == "__main__":
    unittest.main()
