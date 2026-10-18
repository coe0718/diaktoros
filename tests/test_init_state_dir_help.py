#!/usr/bin/env python3
"""Issue #811: ``init --state-dir`` help names the path ``cmd_init`` really writes.

``config.default_state_dir`` is ``~/.hermes/state/diaktoros/<id>`` (``review-loops/<id>`` only
for an install ``migrate`` has not moved yet), so the help must lead with the former and name
the latter only as a leftover.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import commands_doc  # noqa: E402


class InitStateDirHelp(unittest.TestCase):
    def help_text(self) -> str:
        init = commands_doc.verbs(commands_doc.parser())["init"]
        action = next(a for a in init._actions if "--state-dir" in a.option_strings)
        return action.help

    def test_help_names_the_current_default_and_the_old_path_as_leftover(self):
        text = self.help_text()
        self.assertIn("~/.hermes/state/diaktoros/<id>", text)
        self.assertIn("leftover", text)
        self.assertIn("migrate", text)
        # The old path may appear, but only after the current default.
        self.assertLess(text.index("state/diaktoros/<id>"), text.index("state/review-loops/<id>"))
        self.assertTrue(text.startswith("where this loop keeps its state files (default: "
                                        "~/.hermes/state/diaktoros/<id>;"))


if __name__ == "__main__":
    unittest.main()
