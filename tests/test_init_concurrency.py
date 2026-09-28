#!/usr/bin/env python3
"""Issue #76: ``--concurrency N`` reaches both seats, and parallelism without a clone is refused.

``init`` used to write ``seats.<seat>.concurrency`` from the per-seat flags' *defaults*, so every
loop pinned both seats at 1 and the loop-level ``concurrency`` the operator asked for was dead —
and with it the isolation rail, which reads the effective per-seat value. Everything here runs
in-process against the real parser, on the harness fixture (stub GitHub, temp HOME).
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
from review_loop import config  # noqa: E402

LOOP_ID = "conc"
LOOP_FILE = t.LOOPS_DIR / f"{LOOP_ID}.json"


class Base(unittest.TestCase):
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
        t.CLONE.mkdir(parents=True, exist_ok=True)
        LOOP_FILE.unlink(missing_ok=True)
        self.addCleanup(LOOP_FILE.unlink, missing_ok=True)

    def cli(self, *argv, settings=None) -> tuple[int, str]:
        return t.run_cli(t.parser_for(settings).parse_args(list(argv)))

    def init(self, *extra, settings=None) -> tuple[int, str]:
        return self.cli("init", "--repo", t.REPO, "--id", LOOP_ID, "--host", t.HOST,
                        "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                        "--reviewer-profile", "reviewer-profile",
                        "--fixer-profile", "fixer-profile",
                        "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                        "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *t.READER_ARGS, *extra,
                        settings=settings)

    def written(self) -> dict:
        return json.loads(LOOP_FILE.read_text())

    def capacity(self) -> tuple[int, int]:
        loop = config.load_id(LOOP_ID)
        return config.seat_concurrency(loop, "reviewer"), config.seat_concurrency(loop, "fixer")


class InitConcurrencyTest(Base):
    def test_loop_concurrency_reaches_both_seats(self):
        rc, out = self.init("--concurrency", "3", "--clone", str(t.CLONE))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), (3, 3))
        raw = self.written()
        self.assertEqual(raw["concurrency"], 3)
        for seat in ("reviewer", "fixer"):
            self.assertNotIn("concurrency", raw["seats"][seat],
                             "a seat value nobody asked for pins the seat off the loop default")
        self.assertIn("parallel now: reviewer 3 · fixer 3", out)

    def test_a_per_seat_flag_overrides_the_loop_default(self):
        rc, out = self.init("--concurrency", "3", "--fixer-concurrency", "1",
                            "--clone", str(t.CLONE))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), (3, 1))
        raw = self.written()
        self.assertNotIn("concurrency", raw["seats"]["reviewer"])
        self.assertEqual(raw["seats"]["fixer"]["concurrency"], 1)
        # The same note `set` prints, so the pinned seat is never a surprise.
        self.assertIn("note: fixer has its own concurrency (1)", out)
        self.assertIn("parallel now: reviewer 3 · fixer 1", out)

    def test_parallelism_without_a_clone_is_refused(self):
        for flags in (("--concurrency", "2"), ("--reviewer-concurrency", "2")):
            with self.subTest(flags=flags):
                LOOP_FILE.unlink(missing_ok=True)
                rc, out = self.init(*flags)
                self.assertEqual(rc, 2, out)
                self.assertIn("requires 'clone'", out)
                self.assertFalse(LOOP_FILE.exists())

    def test_the_default_is_serialized_and_writes_no_seat_value(self):
        rc, out = self.init()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), (1, 1))
        raw = self.written()
        self.assertEqual(raw["concurrency"], 1)
        for seat in ("reviewer", "fixer"):
            self.assertNotIn("concurrency", raw["seats"][seat])

    def test_differing_settings_seats_are_written(self):
        settings = {"reviewer_concurrency": 2, "fixer_concurrency": 1, "clone": str(t.CLONE)}
        rc, out = self.init(settings=settings)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), (2, 1))
        self.assertNotIn("concurrency", self.written()["seats"]["fixer"])

    def test_an_explicit_loop_flag_wins_over_differing_settings_seats(self):
        settings = {"reviewer_concurrency": 2, "fixer_concurrency": 1, "clone": str(t.CLONE)}
        rc, out = self.init("--concurrency", "3", settings=settings)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), (3, 3))

    def test_agreeing_settings_seats_become_the_loop_default(self):
        settings = {"reviewer_concurrency": 2, "fixer_concurrency": 2, "clone": str(t.CLONE)}
        rc, out = self.init(settings=settings)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), (2, 2))
        self.assertEqual(self.written()["concurrency"], 2)
        self.assertNotIn("concurrency", self.written()["seats"]["fixer"])


class SetConcurrencyTest(Base):
    def test_set_concurrency_moves_a_loop_init_wrote(self):
        rc, out = self.init("--clone", str(t.CLONE))
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--concurrency", "3")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), (3, 3))
        self.assertNotIn("has its own concurrency", out)
        self.assertIn("parallel now: reviewer 3 · fixer 3", out)


class ExistingPinnedConfigTest(Base):
    """A config written before the fix carries ``seats.<seat>.concurrency: 1``. It keeps it."""

    def pinned(self, *, concurrency: int = 3) -> None:
        rc, out = self.init("--clone", str(t.CLONE))
        self.assertEqual(rc, 0, out)
        raw = self.written()
        raw["concurrency"] = concurrency
        for seat in ("reviewer", "fixer"):
            raw["seats"][seat]["concurrency"] = 1
        LOOP_FILE.write_text(json.dumps(raw))

    def test_a_pinned_seat_keeps_its_value_and_status_says_why(self):
        self.pinned()
        self.assertEqual(self.capacity(), (1, 1))
        rc, out = self.cli("status", "--loop", LOOP_ID)
        self.assertEqual(rc, 0, out)
        self.assertIn("parallel:   reviewer 1 (serialized) · fixer 1 (serialized)", out)
        for seat in ("reviewer", "fixer"):
            self.assertIn(f"{seat} is pinned at 1 by seats.{seat}.concurrency", out)
            self.assertIn(f"--{seat}-concurrency 3", out)

    def test_no_note_when_the_seat_agrees_with_the_loop(self):
        self.pinned(concurrency=1)
        rc, out = self.cli("status", "--loop", LOOP_ID)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("is pinned at", out)


if __name__ == "__main__":
    unittest.main()
