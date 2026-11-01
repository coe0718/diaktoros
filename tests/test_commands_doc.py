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

    def test_review_exit_2_names_the_crashed_gate(self):
        """`review` returns 2 for a crashed gate too ("the gate did not finish — it crashed
        (Type)", one_shot_action._review; tests/test_review_entry_point.py), not only a timeout."""
        text = commands_doc.DOC.read_text()
        section = text.split("### review\n", 1)[1].split("<!-- flags:review -->", 1)[0]
        exit2 = " ".join(section.split("Exit 2 when", 1)[1].split("\n\n", 1)[0].split())
        self.assertIn("the gate did not finish", exit2)
        self.assertIn("the gate did not finish — it crashed (Type)", exit2)

    def test_every_command_is_in_the_quick_index(self):
        text = commands_doc.DOC.read_text()
        index = text.split("\n---\n", 1)[0]
        for verb in commands_doc.verbs(commands_doc.parser()):
            self.assertIn(f"(#{verb})", index, f"{verb} is missing from the 'I want to…' table")

    def test_review_exit_2_names_no_deadline_the_gate_lacks(self):
        """`review` runs the gate in-process with no SIGALRM backstop (#1046), so no fixed
        "120 seconds" deadline exists on that path; the doc names the GitHub budget instead and
        sends the operator to `explain` on any non-finish (#1385)."""
        text = commands_doc.DOC.read_text()
        section = text.split("### review\n", 1)[1].split("<!-- flags:review -->", 1)[0]
        self.assertNotIn("120 seconds", section)
        self.assertIn("did not finish", section)
        self.assertIn("check `explain` before asking again", section)
        # A hang outside a GitHub call is not interrupted by any read deadline (#1529 F1).
        flat = " ".join(section.split())
        self.assertNotIn("bounded only by that read", flat)
        self.assertIn("Only the gate's GitHub calls are deadline-limited", flat)
        self.assertIn("a hang outside a GitHub call has no bound", flat)


if __name__ == "__main__":
    unittest.main()
