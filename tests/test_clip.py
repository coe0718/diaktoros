#!/usr/bin/env python3
"""#2104: a held line is cut for ``status`` at its last space, hyphen or em dash in the back half
of what fits. One with no boundary there (a long unbroken check name) keeps what fits and is cut
mid-token, instead of dropping back to a space near the front and showing almost nothing."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros import stats  # noqa: E402

PREFIX = "held: red CI on "


class Clip(unittest.TestCase):
    def why(self, text: str) -> str:
        why = stats._why("waiting", text)
        self.assertLessEqual(len(why), stats.WHY_MAX)
        self.assertTrue(why.endswith("…"), why)
        return why

    def test_an_unbroken_token_keeps_what_fits(self):
        text = PREFIX + "x" * 200
        self.assertEqual(self.why(text), text[:stats.WHY_MAX - 1] + "…")

    def test_a_hyphenated_token_is_cut_at_its_hyphen(self):
        text = PREFIX + "a" * 120 + "-" + "b" * 80
        self.assertEqual(self.why(text), text[:len(PREFIX) + 120] + "…")

    def test_a_sentence_is_still_cut_at_its_last_space(self):
        text = PREFIX + "lint and " * 30
        why = self.why(text)
        self.assertEqual(text[len(why) - 1], " ", why)


if __name__ == "__main__":
    unittest.main()
