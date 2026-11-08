"""#1484: ``init``, ``setup`` and ``set`` declare each seat's flags from one table.

Read from the real ``hermes dk`` parser, as ``register_cli`` builds it: every per-seat flag any
of the three commands has is in ``cli/seat_flags.TABLE``, each command that has it shows the same
help (``init`` and ``setup`` add only the row's ``(default: ...)`` note), and a command without it
is one the table names, with its reason.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import pathlib
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import commands_doc  # noqa: E402

COMMANDS = ("init", "setup", "set")
SEAT = re.compile(r"^--(reviewer|fixer|shadow|triage)-(concurrency|turn-budget|daily-turns|"
                  r"daily-tokens|max-steps|profile)$")
# Seat-named flags the table leaves out on purpose (see seat_flags' docstring): the shadow
# profile means "off" too on `set`, where it shares a group with --no-shadow.
OUTSIDE = {"--shadow-profile"}


def _flags(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    return {s: a for a in parser._actions for s in a.option_strings
            if SEAT.match(s) and s not in OUTSIDE}


class SeatFlagTable(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.verbs = commands_doc.verbs(commands_doc.parser())
        from diaktoros.cli import seat_flags
        cls.table = {row.flag: row for row in seat_flags.TABLE}
        cls.seat_flags = seat_flags

    def test_every_per_seat_flag_is_in_the_table(self):
        for command in COMMANDS:
            for flag in _flags(self.verbs[command]):
                with self.subTest(command=command, flag=flag):
                    self.assertIn(flag, self.table, "declare it in cli/seat_flags.TABLE")

    def test_the_three_commands_show_the_same_help(self):
        seen = {command: _flags(self.verbs[command]) for command in COMMANDS}
        for flag in sorted({f for flags in seen.values() for f in flags}):
            helps = {}
            for command, flags in seen.items():
                if flag in flags:
                    # init and setup add only what an unset flag means on a new loop.
                    helps[command] = re.sub(r" \(default: [^()]*\)$", "", flags[flag].help or "")
            with self.subTest(flag=flag):
                self.assertEqual(len(set(helps.values())), 1, helps)

    def test_a_missing_flag_is_a_named_exception(self):
        for row in self.table.values():
            for command in COMMANDS:
                has = row.flag in _flags(self.verbs[command])
                with self.subTest(flag=row.flag, command=command):
                    self.assertEqual(has, command in row.commands)
                    if not has:
                        self.assertTrue(row.lacks, "name why the command has no such flag")

    def test_the_parsed_values_keep_their_types_and_defaults(self):
        args = self.verbs["set"].parse_args(["--loop", "x", "--shadow-max-steps", "9"])
        self.assertEqual(args.shadow_max_steps, 9)
        self.assertIsNone(args.reviewer_daily_turns)
        args = self.verbs["init"].parse_args(["--repo", "o/r", "--reviewer-concurrency", "2"])
        self.assertEqual((args.reviewer_concurrency, args.fixer_concurrency), (2, None))
        self.assertEqual(args.reviewer_profile, "")
        args = self.verbs["setup"].parse_args(["--fixer-profile", "fp"])
        self.assertEqual((args.fixer_profile, args.reviewer_profile), ("fp", ""))

    def test_setup_passes_every_numeric_seat_flag_on_to_init(self):
        # setup's step 2 is init's command line: a seat flag setup takes and does not forward
        # would be accepted and silently dropped.
        from diaktoros.cli import setup_command
        given = [row for row in self.seat_flags.rows("setup")
                 if row.type is int and "init" in row.commands]
        self.assertTrue(given)
        argv = ["--yes", "--repo", "o/r", "--reviewer", "rv", "--fixer", "fx",
                "--reviewer-profile", "rp", "--fixer-profile", "fp",
                "--reviewer-token", "/t/r", "--fixer-token", "/t/f", "--read-token", "rd",
                "--read-token-file", "/t/rd"]
        for n, row in enumerate(given, start=8):
            argv += [row.flag, str(n)]
        args = self.verbs["setup"].parse_args(argv)
        with mock.patch.object(setup_command, "_ask", lambda q, default, i: default):
            init_argv, _admin = setup_command._setup_init_argv(args, "o/r", "r", False)
        for n, row in enumerate(given, start=8):
            with self.subTest(flag=row.flag):
                self.assertIn(f"{row.flag}={n}", init_argv)


if __name__ == "__main__":
    unittest.main()
