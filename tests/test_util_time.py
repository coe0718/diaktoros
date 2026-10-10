"""#563: an unreadable time is unknown (None), never age 0."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import util  # noqa: E402
from scripts import watchdog  # noqa: E402


class Age(unittest.TestCase):
    def test_unreadable_or_missing_is_unknown_and_past_every_grace(self):
        for bad in (None, "", "garbage", "2026-13-99T99:99:99Z"):
            with self.subTest(bad=bad):
                self.assertIsNone(util.age_min(bad))
                self.assertTrue(watchdog.past(util.age_min(bad), 10_000))
        self.assertIn("unreadable", watchdog.ago(None))

    def test_a_real_time_ages_normally(self):
        mins = util.age_min("2020-01-01T00:00:00Z")
        self.assertGreater(mins, 0)
        self.assertFalse(watchdog.past(0.5, 1.0))
        self.assertTrue(watchdog.past(2.0, 1.0))


if __name__ == "__main__":
    unittest.main()
