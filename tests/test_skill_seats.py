"""skill/SKILL.md mirrors the protocol of every seat the run ledger knows (#1069)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import run_supervisor

SKILL = (ROOT / "skill" / "SKILL.md").read_text()
# The heading each seat's section carries: the seat name, spelled as the doc spells it.
HEADING = {"reviewer": "the reviewer", "fixer": "the fixer", "adjudicator": "the adjudicator",
           "triage": "triage", "issue_fixer": "the issue fixer", "shadow": "the shadow"}


def section(seat: str) -> str:
    match = re.search(rf"^## If you are {HEADING[seat]}\n(.*?)(?=^## )", SKILL, re.S | re.M)
    return match.group(1) if match else ""


class SeatSections(unittest.TestCase):
    def test_every_seat_has_a_section(self):
        self.assertEqual(set(HEADING), set(run_supervisor.SEATS), "a seat was added or removed")
        for seat in run_supervisor.SEATS:
            with self.subTest(seat=seat):
                self.assertTrue(section(seat).strip(), f"SKILL.md has no 'If you are' section for {seat}")

    def test_shadow_section_states_the_no_write_protocol_and_where_the_verdict_goes(self):
        text = section("shadow")
        for needle in ("no-write", "operation out of scope", "posts nothing", "shadow_reviews",
                       "snapshot", "stats", "shadow.escalate", "shadow.file_issues",
                       "Not verified:"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_shadow_section_separates_automatic_retry_from_the_operators_manual_one(self):
        # A failed shadow is never retried automatically, but `retry --seat shadow` re-arms it.
        text = " ".join(section("shadow").split())
        self.assertIn("not retried automatically", text)
        self.assertIn("hermes dk retry --loop LOOP --pr N --seat shadow", text)
        self.assertNotIn("is not retried.", text)


if __name__ == "__main__":
    unittest.main()
