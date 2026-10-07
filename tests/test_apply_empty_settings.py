#!/usr/bin/env python3
"""#59: ``apply`` with an empty settings form must not reset cap/concurrency to defaults.

The settings form only ever names what the operator typed. A blank/absent field means "not set
here", never "reset to the schema default". ``apply_settings`` used to unconditionally recompute
``cap`` and ``concurrency`` (and the other knobs) from ``settings_defaults`` — which substitutes the
schema default for every ``None``/``""`` — so submitting the form empty printed ``cap: 5 → 3`` and
``reviewer concurrency: 2 → 1`` and wrote them over a loop's explicit numbers. These tests pin the
contract: a value the form sets sticks; an empty form preserves the loop's own cap/concurrency.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros import config  # noqa: E402


def _loop(cap: int, concurrency: int) -> dict:
    """A raw (pre-``normalize``) loop already carrying explicit, non-default cap/concurrency."""
    return {"id": "widgets", "repo": "acme/widgets", "cap": cap, "concurrency": concurrency,
            "fixers": ["fixer"], "reviewers": ["reviewer"], "read_token": "reader",
            "clone": "/some/where", "base": "main",
            "seats": {"reviewer": {"profile": "rev", "route": "r"},
                      "fixer": {"profile": "fix", "route": "f"}}}


class ApplyEmptySettingsTest(unittest.TestCase):
    def test_apply_settings_sets_cap_and_concurrency_when_the_form_has_them(self):
        # A non-empty form genuinely sets the knobs: cap=X, reviewer/fixer concurrency=Y.
        loop = _loop(cap=2, concurrency=1)
        settings = {"cap": 5, "reviewer_concurrency": 2, "fixer_concurrency": 2}
        overlaid = config.apply_settings(loop, settings)
        self.assertEqual(config.normalize(overlaid)["cap"], 5)
        self.assertEqual(config.normalize(overlaid)["concurrency"], 2)

    def test_an_empty_form_preserves_the_loops_cap_and_concurrency(self):
        # Step 1: a non-empty form sets cap=X/concurrency=Y and they stick.
        cap, concurrency = 5, 2
        loop = _loop(cap=2, concurrency=1)
        loop = dict(config.apply_settings(
            loop, {"cap": cap, "reviewer_concurrency": concurrency,
                   "fixer_concurrency": concurrency}))
        self.assertEqual((config.normalize(loop)["cap"],
                          config.normalize(loop)["concurrency"]), (cap, concurrency))

        # Step 2: an empty form must NOT reset cap/concurrency to the schema defaults (3 / 1).
        kept = config.apply_settings(loop, {})
        self.assertEqual(config.normalize(kept)["cap"], cap,
                         "empty settings reset 'cap' back to the default")
        self.assertEqual(config.normalize(kept)["concurrency"], concurrency,
                         "empty settings reset 'concurrency' back to the default")
        # Same answer when the caller passes no settings dict at all (None == nothing set).
        kept_none = config.apply_settings(loop, None)
        self.assertEqual(config.normalize(kept_none)["cap"], cap)
        self.assertEqual(config.normalize(kept_none)["concurrency"], concurrency)

    def test_an_empty_form_preserves_per_seat_concurrency_pins(self):
        # A loop whose seats carry their own pins keeps them when the form comes back blank.
        loop = _loop(cap=4, concurrency=1)
        loop["seats"]["reviewer"]["concurrency"] = 3
        loop["seats"]["fixer"]["concurrency"] = 2
        kept = config.apply_settings(loop, {})
        self.assertEqual(config.normalize(kept)["cap"], 4)
        self.assertEqual(config.normalize(kept)["concurrency"], 1)
        self.assertEqual(config.seat_concurrency(kept, "reviewer"), 3)
        self.assertEqual(config.seat_concurrency(kept, "fixer"), 2)


if __name__ == "__main__":
    unittest.main()
