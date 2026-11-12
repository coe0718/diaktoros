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
from diaktoros import config, routes  # noqa: E402
from diaktoros.cli import setup_command as cli_setup  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402

SEATS = ("shadow", "triage")


class ShadowTriageConcurrency(Base):
    def setUp(self):
        super().setUp()
        # Shadow/triage above 1 needs isolation like any seat (#1693): these tests drive the flag
        # plumbing, so give them the isolated worker's runtime file, as `setup` would.
        runtime = config.host_path("runtime")
        if not runtime.exists():
            runtime.write_text("{}")
            runtime.chmod(0o600)
            self.addCleanup(runtime.unlink, missing_ok=True)

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

    def test_the_capacity_summary_names_every_seat_set_can_move(self):
        """Issue #1129: the summary printed after the delta lines listed reviewer and fixer only."""
        rc, out = self.init("--shadow-concurrency", "3", "--triage-concurrency", "2")
        self.assertEqual(rc, 0, out)
        self.assertIn("parallel now: reviewer 1 · fixer 1 · shadow 3 · triage 2", out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-concurrency", "4",
                           "--triage-concurrency", "5")
        self.assertEqual(rc, 0, out)
        self.assertIn("parallel now: reviewer 1 · fixer 1 · shadow 4 · triage 5", out)

    def test_a_seat_nobody_configured_stays_out_of_the_summary(self):
        rc, out = self.init()
        self.assertEqual(rc, 0, out)
        self.assertIn("parallel now: reviewer 1 · fixer 1   (", out)
        self.assertNotIn("shadow 1", out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--cap", "4")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("shadow", out.split("parallel now:")[-1])

    def test_status_list_and_doctor_agree_with_the_summary(self):
        from unittest import mock
        from diaktoros import contained, doctor
        rc, out = self.init("--shadow-concurrency", "3", "--triage-concurrency", "2")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("status", "--loop", LOOP_ID)
        self.assertEqual(rc, 0, out)
        self.assertIn("parallel:   reviewer 1 (serialized) · fixer 1 (serialized) · shadow 3 · "
                      "triage 2", out)
        rc, out = self.cli("list")
        self.assertIn("reviewer=1 fixer=1 shadow=3 triage=2", out)
        with mock.patch.object(contained, "IGNORED_SIZE_OVERRIDES", []), \
                mock.patch.object(doctor, "_host_memory",
                                  return_value=(64 * 1024 ** 3, 64 * 1024 ** 3)):
            check = doctor.check_sandbox_caps(config.load_id(LOOP_ID))
        self.assertIn("8 concurrent turns", check.detail)    # 1 + 1 + 3 + 2, plus the adjudicator

    def test_an_enabled_but_unpinned_seat_is_named_with_its_default(self):
        """The seat runs, so the summary states its capacity (1) though nobody set a value."""
        (config.profiles_root() / "arbiter").mkdir(parents=True, exist_ok=True)
        rc, out = self.init("--shadow-profile", "critic", "--shadow-every", "1")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("concurrency", self.written()["seats"].get("shadow", {}))
        self.assertIn("parallel now: reviewer 1 · fixer 1 · shadow 1   (", out)
        rc, out = self.cli("status", "--loop", LOOP_ID)
        self.assertIn("reviewer 1 (serialized) · fixer 1 (serialized) · shadow 1 (serialized)", out)
        rc, out = self.cli("list")
        self.assertIn("reviewer=1 fixer=1 shadow=1 ", out)
        self.assertNotIn("triage=", out)
        self.addCleanup(lambda: routes.restore_entries({f"{LOOP_ID}-triage": None}))
        rc, out = self.cli("triage", "--loop", LOOP_ID, "--enable", "--triage-profile", "arbiter",
                           "--author", "Owner", "--labels", "bug,docs,P1", "--maintainer", "owner")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("concurrency", self.written()["seats"].get("triage", {}))
        rc, out = self.cli("set", "--loop", LOOP_ID, "--cap", "4")
        self.assertEqual(rc, 0, out)
        self.assertIn("parallel now: reviewer 1 · fixer 1 · shadow 1 · triage 1   (", out)
        rc, out = self.cli("status", "--loop", LOOP_ID)
        self.assertIn("shadow 1 (serialized) · triage 1 (serialized)", out)
        rc, out = self.cli("list")
        self.assertIn("reviewer=1 fixer=1 shadow=1 triage=1 ", out)

    def test_capacity_seats_selects_on_enabled_or_pinned(self):
        base = {"id": "t", "seats": {}}
        self.assertEqual(config.capacity_seats(base), ("reviewer", "fixer"))
        self.assertEqual(config.capacity_seats({**base, "shadow": {"profile": "critic"}}),
                         ("reviewer", "fixer", "shadow"))
        self.assertEqual(config.capacity_seats({**base, "triage": {"route": "r"}}),
                         ("reviewer", "fixer", "triage"))
        self.assertEqual(config.capacity_seats({"id": "t", "seats": {"shadow": {"concurrency": 2},
                                                                     "triage": {"concurrency": 2}}}),
                         ("reviewer", "fixer", "shadow", "triage"))

    def sandbox_caps(self, loop, available_gib):
        from unittest import mock
        from diaktoros import contained, doctor
        with mock.patch.object(contained, "IGNORED_SIZE_OVERRIDES", []), \
                mock.patch.object(doctor, "_host_memory",
                                  return_value=(available_gib * 1024 ** 3, 64 * 1024 ** 3)):
            return doctor.check_sandbox_caps(loop)

    def test_sandbox_caps_counts_the_adjudicators_own_concurrency(self):
        """Review of #1629 F1: shadow 3 + adjudicator 4 + reviewer/fixer 1 is 9 turns, not 6."""
        loop = {"id": "t", "shadow": {"profile": "critic"},
                "seats": {"shadow": {"concurrency": 3}, "adjudicator": {"concurrency": 4}}}
        self.assertEqual(config.worst_case_turns(loop), 9)
        per_turn = 10                                  # GiB: /work 8 + /tmp 2 at the defaults
        check = self.sandbox_caps(loop, 9 * per_turn - 1)         # one turn short of the worst case
        self.assertEqual(check.status, "mismatch", check.detail)
        self.assertIn("9 concurrent turns", check.detail)
        check = self.sandbox_caps(loop, 9 * per_turn)
        self.assertEqual(check.status, "verified", check.detail)
        # No adjudicator concurrency set: the one the check always allowed for.
        self.assertEqual(config.worst_case_turns({"id": "t"}), 3)

    def test_sandbox_caps_counts_the_issue_fixer_when_issue_fixes_are_on(self):
        from unittest import mock
        loop = {"id": "t", "triage": {"route": "r", "fix_label": "agent-fix"}, "seats": {}}
        with mock.patch.object(config, "unattended_fixer_push_enabled", return_value=True):
            self.assertEqual(config.worst_case_turns(loop), 5)   # reviewer, fixer, triage, adjudicator, issue fixer
        self.assertEqual(config.worst_case_turns(loop), 4)

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
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--shadow-concurrency=3", argv)
        self.assertIn("--triage-concurrency=2", argv)
        bare = t.parser_for({}).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli_setup._setup_init_argv(bare, t.REPO, LOOP_ID, interactive=False)
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

    def test_docs_do_not_call_triage_concurrency_file_only(self):
        """#1128: the docs name the paths that write it, and the inheritance rule leaves it out."""
        docs = pathlib.Path(__file__).resolve().parent.parent / "docs"
        read = lambda name: " ".join((docs / name).read_text().split())  # noqa: E731
        config_md, commands_md, settings_md = read("configuration.md"), read("commands.md"), read("settings.md")
        for stale in ("Triage concurrency and turn budget are also file-only",
                      "Concurrency and turn budget must be edited in the loop file",
                      "Triage capacity and budget are file-only settings"):
            self.assertNotIn(stale, config_md + commands_md)
        self.assertIn("| `seats.shadow.concurrency` |", config_md)
        self.assertNotIn("both form concurrency fields", settings_md)
        self.assertIn("`shadow_concurrency` and `triage_concurrency`, are not part of this rule", settings_md)


if __name__ == "__main__":
    unittest.main()
