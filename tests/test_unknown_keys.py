"""#540: a loop file key nothing reads is refused, not silently ignored."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros import config

BASE = {"repo": "o/n", "fixers": ["f"], "reviewers": ["r"], "read_token": "rd",
        "tokens": {"rd": "/x"},
        "seats": {"reviewer": {"route": "a", "profile": "p"},
                  "fixer": {"route": "b", "profile": "q"}}}


class UnknownKeys(unittest.TestCase):
    def test_valid_loads(self):
        self.assertEqual(config.normalize(dict(BASE))["repo"], "o/n")

    def test_top_level_typo_refused(self):
        with self.assertRaises(config.ConfigError) as ctx:
            config.normalize({**BASE, "review-after-ci": True})
        self.assertIn("review-after-ci", str(ctx.exception))
        self.assertIn("review_after_ci", str(ctx.exception))
        with self.assertRaises(config.ConfigError):
            config.normalize({**BASE, "fix_ci ": "on"})

    def test_seat_typo_refused(self):
        seats = {**BASE["seats"], "fixer": {"route": "b", "profile": "q", "turn-budget-s": 5}}
        with self.assertRaises(config.ConfigError) as ctx:
            config.normalize({**BASE, "seats": seats})
        self.assertIn("seats.fixer", str(ctx.exception))
        self.assertIn("turn_budget_s", str(ctx.exception))

    def test_load_file_refuses(self):
        path = pathlib.Path(tempfile.mkdtemp()) / "n.json"
        path.write_text(json.dumps({**BASE, "grace-min": 30}))
        with self.assertRaises(config.ConfigError) as ctx:
            config.load_file(path)
        self.assertIn("grace-min", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
