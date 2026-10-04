"""docs/commands.md covers every command, and its flag tables match the live parser.

The tables are generated (``python3 tests/commands_doc.py --write``); this test fails when a flag
changed without regenerating them, or when a new command has no section — so the reference a
newcomer reads can never quietly fall behind the CLI they run.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import commands_doc  # noqa: E402


class CommandsDoc(unittest.TestCase):
    def test_every_command_has_a_section_and_every_table_is_current(self):
        root = commands_doc.parser()
        text = commands_doc.DOC.read_text()
        self.assertEqual(commands_doc.missing(text, root), [],
                         "add a section with a <!-- flags:VERB --> marker to docs/commands.md")
        self.assertEqual(commands_doc.render(text, root), text,
                         "flag tables are stale: run `python3 tests/commands_doc.py --write`")

    def test_no_flag_is_one_hermes_takes_for_itself(self):
        """`hermes` reads -p/--profile from anywhere on its command line, after the subcommand
        too, and switches its own profile: `triage --profile vex` ran in vex's Hermes home, where
        the plugin was not even loaded. A plugin flag of that name can never reach the plugin."""
        taken = [f"{verb} {flag}"
                 for verb, sub in commands_doc.verbs(commands_doc.parser()).items()
                 for action in sub._actions for flag in action.option_strings
                 if flag in ("-p", "--profile")]
        self.assertEqual(taken, [])

    def test_every_flag_says_what_it_does(self):
        """A flag with no help is a blank cell in the reference and in --help (#236)."""
        blank = [f"{verb} {action.option_strings[0]}"
                 for verb, sub in commands_doc.verbs(commands_doc.parser()).items()
                 for action in sub._actions
                 if action.option_strings and not isinstance(action, commands_doc.argparse._HelpAction)
                 and not (action.help or "").strip()]
        self.assertEqual(blank, [], "give these flags help= text, then run "
                                    "`python3 tests/commands_doc.py --write`")

    def test_every_command_is_in_the_quick_index(self):
        text = commands_doc.DOC.read_text()
        index = text.split("\n---\n", 1)[0]
        for verb in commands_doc.verbs(commands_doc.parser()):
            self.assertIn(f"(#{verb})", index, f"{verb} is missing from the 'I want to…' table")


if __name__ == "__main__":
    unittest.main()
