#!/usr/bin/env python3
"""#271: how many agent steps a seat's turn may take is a setting, not a code constant.

A fixer that has to find its way into the code from an issue or a review spent the old fixed 24
steps exploring and published nothing. Each seat now has a ``max_steps`` (``set
--reviewer-max-steps`` / ``--fixer-max-steps``, or the loop file for the adjudicator and triage);
unset, the role's default applies. An issue fix runs as the fixer seat and takes its value. The
turn's model-call quota follows from the steps, and the proxy's ceiling covers the largest one.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from review_loop import config, inference_proxy  # noqa: E402

LOOP_ID = "steps"


class Resolution(unittest.TestCase):
    def test_role_defaults_and_seat_values(self):
        self.assertEqual(config.max_steps({}, "reviewer"), 60)
        self.assertEqual(config.max_steps({}, "fixer"), 80)
        self.assertEqual(config.max_steps({}, "issue_fixer"), 80)
        loop = {"seats": {"fixer": {"max_steps": 150}, "triage": {"max_steps": 12}}}
        self.assertEqual(config.max_steps(loop, "issue_fixer"), 150)   # runs as the fixer seat
        self.assertEqual(config.max_steps(loop, "triage"), 12)
        self.assertEqual(config.max_steps(loop, "reviewer"), 60)

    def test_model_calls_follow_steps_and_fit_the_proxy(self):
        self.assertEqual([config.model_calls(n) for n in (8, 24, 80, 200)], [16, 32, 100, 250])
        self.assertEqual(config.model_calls(config.MAX_STEPS_RANGE[1]), inference_proxy.MAX_CALLS)

    def test_the_loop_file_is_checked(self):
        for bad in (0, 7, 201, True, "40", 1.5):
            with self.subTest(value=bad), self.assertRaisesRegex(config.ConfigError, "max_steps"):
                config._check_max_steps(bad, "seats.fixer.max_steps", "f")
        self.assertEqual(config._triage_seat({"max_steps": 12}, "f"), {"max_steps": 12})
        with self.assertRaisesRegex(config.ConfigError, "max_steps"):
            config._triage_seat({"max_steps": 300}, "f")


class Cli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={})
        self.file = config.config_dir() / f"{LOOP_ID}.json"
        self.file.unlink(missing_ok=True)
        self.addCleanup(self.file.unlink, missing_ok=True)
        rc, out = self.cli("init", "--repo", t.REPO, "--id", LOOP_ID, "--host", t.HOST,
                           "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                           "--reviewer-profile", "reviewer-profile",
                           "--fixer-profile", "fixer-profile",
                           "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                           "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *t.READER_ARGS)
        self.assertEqual(rc, 0, out)

    def cli(self, *argv) -> tuple[int, str]:
        return t.run_cli(t.parser_for(None).parse_args(list(argv)))

    def seats(self) -> dict:
        return json.loads(self.file.read_text())["seats"]

    def test_set_moves_a_seats_steps_and_zero_goes_back_to_the_default(self):
        rc, out = self.cli("set", "--loop", LOOP_ID, "--fixer-max-steps", "150")
        self.assertEqual(rc, 0, out)
        self.assertIn("fixer max steps: 80 → 150", out)
        self.assertEqual(self.seats()["fixer"]["max_steps"], 150)
        rc, out = self.cli("status", "--loop", LOOP_ID)
        self.assertIn("steps:      reviewer 60 · fixer 150", out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--fixer-max-steps", "0")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("max_steps", self.seats()["fixer"])
        self.assertEqual(config.max_steps(config.load_id(LOOP_ID), "fixer"), 80)

    def test_set_refuses_a_value_out_of_range_and_writes_nothing(self):
        before = self.file.read_bytes()
        rc, out = self.cli("set", "--loop", LOOP_ID, "--reviewer-max-steps", "500")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("max_steps", out)
        self.assertEqual(self.file.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
