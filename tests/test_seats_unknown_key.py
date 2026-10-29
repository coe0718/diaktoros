"""#814: an unknown key directly under ``seats`` is refused, not silently rebuilt away."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from diaktoros import config  # noqa: E402
from diaktoros.config import blocks  # noqa: E402


def _raw(**seats) -> dict:
    return {"id": "widgets", "repo": "acme/widgets", "fixers": ["fixer"], "reviewers": ["reviewer"],
            "read_token": "reader",
            "seats": {"reviewer": {"profile": "rev", "route": "r"},
                      "fixer": {"profile": "fix", "route": "f"}, **seats}}


class UnknownSeatName(unittest.TestCase):
    def test_misspelled_seat_name_is_refused_on_load(self):
        with self.assertRaises(config.ConfigError) as cm:
            config.normalize(_raw(reveiwer={"profile": "x", "route": "y"}))
        self.assertIn("seats.'reveiwer'", str(cm.exception))
        self.assertIn("did you mean 'reviewer'", str(cm.exception))

    def test_unrelated_seat_name_is_refused(self):
        with self.assertRaises(config.ConfigError):
            config.normalize(_raw(observer={"profile": "x"}))

    def test_known_seats_still_load(self):
        loop = config.normalize(_raw(triage={"concurrency": 1}, shadow={"concurrency": 1}, adjudicator={"concurrency": 1}))
        self.assertTrue({"reviewer", "fixer", "triage", "shadow"} <= set(loop["seats"]))

    def test_normalized_output_reads_back(self):
        loop = config.normalize(_raw())
        blocks.check_known_keys({"seats": loop["seats"]}, "x")


if __name__ == "__main__":
    unittest.main()
