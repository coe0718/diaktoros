#!/usr/bin/env python3
"""#781: ``apply`` compares every settings-form key it writes, including the nested ones.

Driven through the real ``apply`` command: a form push that moves only ``auto_fix_labels``,
``auto_fix_daily`` or ``observer_weekly_report`` is a detected change and is written, never
reported as "already matches".
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import re
import shlex
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import config, routes  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402
from test_init_concurrency import Base, LOOP_FILE, LOOP_ID  # noqa: E402


class ApplyNestedKeys(Base):
    def setUp(self):
        super().setUp()
        (config.profiles_root() / "arbiter").mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.init()[0], 0)
        for route in (f"{LOOP_ID}-triage", f"{LOOP_ID}-observe"):
            self.addCleanup(lambda r=route: routes.restore_entries({r: None}))
        rc, out = self.cli("triage", "--loop", LOOP_ID, "--enable", "--triage-profile", "arbiter",
                           "--author", "Owner", "--labels", "bug,docs,P1", "--fix-label", "agent-fix",
                           "--maintainer", "owner")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--observer-profile", "arbiter",
                           "--observer-deliver", "telegram")
        self.assertEqual(rc, 0, out)
        self.assertTrue(config.load_id(LOOP_ID)["observer"]["route"])

    def push(self, settings):
        before = self.written()
        loop = config.load_id(LOOP_ID)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings=settings)
        return rc, out, before, loop

    def check(self, settings, section, key, expected):
        rc, out, before, loop = self.push(settings)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("already matches", out)
        self.assertIn(f"{section}.{key}", out)
        self.assertEqual(self.written()[section][key], expected)
        # The second push has nothing left to do.
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings=settings)
        self.assertIn("already matches", out)

    def test_auto_fix_labels(self):
        self.check({"auto_fix_labels": "bug, docs"}, "triage", "auto_fix_labels", ["bug", "docs"])

    def test_auto_fix_daily(self):
        # auto_fix_daily needs labels already on the loop: the push moves only the cap.
        rc, out = self.cli("triage", "--loop", LOOP_ID, "--auto-fix-label", "bug")
        self.assertEqual(rc, 0, out)
        self.check({"auto_fix_daily": "4"}, "triage", "auto_fix_daily", 4)

    def test_fix_daily_turns_without_a_fix_label(self):
        # The loader and `triage --fix-daily-turns` keep the cap with issue fixes off (#1218), so
        # the form's push must not refuse it (#1220).
        rc, out = self.cli("triage", "--loop", LOOP_ID, "--fix-label", "")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("fix_label", config.load_id(LOOP_ID)["triage"])
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"fix_daily_turns": "9"})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("already matches", out)
        self.assertEqual(self.written()["triage"]["fix_daily_turns"], 9)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"fix_daily_turns": "9"})
        self.assertIn("already matches", out)
        self.assertNotIn("fix_label", self.written()["triage"])
        self.assertEqual(config.load_id(LOOP_ID)["triage"]["fix_daily_turns"], 9)

    def test_fix_daily_turns_needs_the_triage_block(self):
        rc, out = self.cli("triage", "--loop", LOOP_ID, "--disable")
        self.assertEqual(rc, 0, out)
        before = self.written()
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"fix_daily_turns": "9"})
        self.assertNotEqual(rc, 0, out)
        self.assertIn("needs issue triage on", out)
        self.assertNotIn("triage.fix_label", out)
        self.assertEqual(self.written(), before)

    def test_the_refusals_triage_command_turns_triage_on(self):
        # #1728: the form's refusal named a bare `hermes dk triage --enable`, itself refused
        # (no --loop, profile, authors or labels). The named command, run, must turn triage on.
        rc, out = self.cli("triage", "--loop", LOOP_ID, "--disable")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"fix_daily_turns": "9"})
        self.assertNotEqual(rc, 0, out)
        found = re.search(r"`hermes dk (triage [^`]*)`", out)
        self.assertIsNotNone(found, out)
        for flag in ("--loop", "--enable", "--triage-profile", "--author", "--labels"):
            self.assertIn(flag, found.group(1).split(), out)
        filled = {"P": "arbiter", "A": "Owner", "L": "bug,docs"}
        rc, out = self.cli(*[filled.get(word, word) for word in shlex.split(found.group(1))])
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"fix_daily_turns": "9"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written()["triage"]["fix_daily_turns"], 9)

    def test_every_refusal_names_the_same_triage_command(self):
        # #1728: one spelling, from config.triage_enable_command, at every site that names it.
        rc, out = self.cli("triage", "--loop", LOOP_ID, "--disable")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"fix_daily_turns": "9"})
        self.assertIn(f"`{config.triage_enable_command(LOOP_ID)}`", out)
        rc, out = self.cli("triage", "--loop", LOOP_ID, "--fix-daily-turns", "9")
        self.assertEqual(rc, 2, out)
        self.assertIn(f"add --enable (with {', '.join(config.TRIAGE_ENABLE_FLAGS)})", out)
        docs = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "settings.md").read_text()
        self.assertIn(config.triage_enable_command(then="--fix-daily-turns N")
                      .removeprefix("hermes dk "), docs)

    def test_observer_weekly_report(self):
        self.check({"observer_weekly_report": "tue 08:00"}, "observer", "weekly_report", "tue 08:00")

    def test_no_form_key_the_push_changes_is_reported_as_matching(self):
        settings = {"auto_fix_labels": "bug", "auto_fix_daily": "3",
                    "observer_weekly_report": "fri 17:30"}
        loop = config.load_id(LOOP_ID)
        pushed = config.normalize(settings_form.apply_settings(loop, settings))
        self.assertNotEqual(pushed, loop)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings=settings)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("already matches", out)
        self.assertEqual(config.load_id(LOOP_ID), pushed)


if __name__ == "__main__":
    unittest.main()
