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

    def test_every_command_is_in_the_quick_index(self):
        text = commands_doc.DOC.read_text()
        index = text.split("\n---\n", 1)[0]
        for verb in commands_doc.verbs(commands_doc.parser()):
            self.assertIn(f"(#{verb})", index, f"{verb} is missing from the 'I want to…' table")


if __name__ == "__main__":
    unittest.main()
