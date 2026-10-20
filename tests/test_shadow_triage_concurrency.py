#!/usr/bin/env python3
"""Issue #1007: ``seats.shadow.concurrency`` and ``seats.triage.concurrency`` have a flag on
``set``, ``init`` and ``setup``, a settings-form field that ``apply`` moves, and a row in ``show``.

The loader accepted both and the run ledger enforced them (``_capacity_for``), but only a hand edit
of the loop file could set them. Each test drives one path through the real parser and command.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from test_init_concurrency import LOOP_FILE, LOOP_ID, Base  # noqa: E402
from diaktoros import config  # noqa: E402
from diaktoros.cli import install as cli_install  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402

SEATS = ("shadow", "triage")


class ShadowTriageConcurrency(Base):
    def seat_value(self, seat):
        return self.written().get("seats", {}).get(seat, {}).get("concurrency")

    def capacity(self):                                   # noqa: D401 (overrides the base's pair)
        loop = config.load_id(LOOP_ID)
        return {seat: config.seat_concurrency(loop, seat) for seat in SEATS}

    def test_init_flags_write_each_seats_own_value(self):
        rc, out = self.init("--shadow-concurrency", "3", "--triage-concurrency", "2")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), {"shadow": 3, "triage": 2})
        self.assertEqual(self.written()["concurrency"], 1)       # the loop default is untouched

    def test_init_without_them_writes_no_seat_value(self):
        rc, out = self.init()
        self.assertEqual(rc, 0, out)
        for seat in SEATS:
            self.assertNotIn(seat, self.written()["seats"])

    def test_init_takes_the_settings_form_and_a_flag_beats_it(self):
        settings = {"shadow_concurrency": "4", "triage_concurrency": "2"}
        rc, out = self.init(settings=settings)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), {"shadow": 4, "triage": 2})
        LOOP_FILE.unlink()
        rc, out = self.init("--shadow-concurrency", "5", settings=settings)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), {"shadow": 5, "triage": 2})

    def test_init_refuses_below_one_and_writes_nothing(self):
        for flag in ("--shadow-concurrency", "--triage-concurrency"):
            with self.subTest(flag=flag):
                rc, out = self.init(flag, "0")
                self.assertEqual(rc, 2, out)
                self.assertIn("must be >= 1", out)
                self.assertFalse(LOOP_FILE.exists())

    def test_init_refuses_a_form_value_below_one(self):
        rc, out = self.init(settings={"triage_concurrency": "0"})
        self.assertEqual(rc, 2, out)
        self.assertIn("seats.triage.concurrency must be >= 1", out)
        self.assertFalse(LOOP_FILE.exists())

    def test_set_moves_each_seat_and_names_the_change(self):
        rc, out = self.init()
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-concurrency", "3",
                           "--triage-concurrency", "2")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), {"shadow": 3, "triage": 2})
        self.assertIn("shadow concurrency: 1 → 3", out)
        self.assertIn("triage concurrency: 1 → 2", out)
        # The loop default does not reach these seats, and `set --concurrency` leaves them alone.
        rc, out = self.cli("set", "--loop", LOOP_ID, "--concurrency", "2", "--clone", str(t.CLONE))
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), {"shadow": 3, "triage": 2})

    def test_set_back_to_one_and_refusals(self):
        rc, out = self.init("--shadow-concurrency", "3")
        self.assertEqual(rc, 0, out)
        for bad in ("0", "-1"):
            rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-concurrency", bad)
            self.assertEqual(rc, 2, out)
            self.assertIn("seats.shadow.concurrency must be >= 1", out)
        self.assertEqual(self.seat_value("shadow"), 3)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-concurrency", "1")
        self.assertEqual((rc, self.capacity()["shadow"]), (0, 1), out)

    def test_apply_moves_them_and_refuses_a_bad_value(self):
        rc, out = self.init()
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("apply", "--loop", LOOP_ID,
                           settings={"shadow_concurrency": "3", "triage_concurrency": " 2 "})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.capacity(), {"shadow": 3, "triage": 2})
        self.assertIn("shadow concurrency: 1 → 3", out)
        # Blank = not set here: the next apply leaves both exactly as they are.
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"shadow_concurrency": ""})
        self.assertEqual(self.capacity(), {"shadow": 3, "triage": 2})
        for bad in ("0", "many"):
            rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"triage_concurrency": bad})
            self.assertNotEqual(rc, 0, out)
            self.assertEqual(self.seat_value("triage"), 2)

    def test_apply_does_not_say_already_matches_over_a_change(self):
        rc, out = self.init()
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("apply", "--loop", LOOP_ID, "--dry-run",
                           settings={"shadow_concurrency": "3"})
        self.assertNotIn("already matches", out)
        self.assertIn("shadow concurrency: 1 → 3", out)
        self.assertEqual(self.seat_value("shadow"), None)         # a dry run writes nothing

    def test_setup_hands_init_the_flags(self):
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--shadow-concurrency", "3",
                                            "--triage-concurrency", "2"])
        argv, _ = cli_install._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--shadow-concurrency=3", argv)
        self.assertIn("--triage-concurrency=2", argv)
        bare = t.parser_for({}).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli_install._setup_init_argv(bare, t.REPO, LOOP_ID, interactive=False)
        self.assertFalse([a for a in argv if "-concurrency" in a])

    def test_show_lists_them_under_the_forms_names(self):
        rc, out = self.init("--shadow-concurrency", "3")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("show", "--loop", LOOP_ID, "--json")
        self.assertEqual(rc, 0, out)
        rows = {r["name"]: r for r in json.loads(out)["settings"]}
        self.assertEqual((rows["shadow_concurrency"]["value"], rows["shadow_concurrency"]["source"]),
                         (3, "loop file"))
        self.assertEqual((rows["triage_concurrency"]["value"], rows["triage_concurrency"]["source"]),
                         (1, "derived"))
        self.assertNotIn("seats.shadow.concurrency", rows)
        self.assertNotIn("seats.triage.concurrency", rows)

    def test_the_schema_and_the_loader_agree(self):
        for seat in SEATS:
            self.assertIn(f"{seat}_concurrency", settings_form.SETTINGS_SCHEMA)
        with self.assertRaises(config.ConfigError):
            config.check_concurrency(0, "seats.shadow.concurrency", "t")
        self.assertEqual(config.check_concurrency("3", "seats.shadow.concurrency", "t"), 3)


if __name__ == "__main__":
    unittest.main()
