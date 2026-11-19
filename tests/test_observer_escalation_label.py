"""The ``escalation`` notice's label is true for every breach the gate reports (#2460).

``gate.breach`` sends one ``escalation`` event for a spent cap and for an escalation raised before
it (the fixer's dispute, the shadow review, the cycle and the operator). The label comes from the
event alone, so it must not claim the cap was spent; the outcome clause says why it stopped."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from diaktoros import observer  # noqa: E402

LOOP = {"id": "widgets", "repo": "o/widgets", "cap": 3}
HEAD = "a" * 40
# The outcome clauses ``gate.breach`` writes for each kind of marker (observe_reserved).
BEFORE_CAP = ("the fixer disputed the review at 1/3 verdicts",
              "the shadow review requested changes at an approved head",
              "a blocking finding asks for this PR's own work at 1/3 verdicts",
              "escalated by the operator at 1/3 verdicts")
CAP = "3/3 verdicts, no approval"


class EscalationLabel(unittest.TestCase):
    def line(self, outcome):
        return observer.summarize(LOOP, "escalation", 7, HEAD, outcome=outcome,
                                  next_turn="adjudicator delivery pending")

    def test_an_escalation_before_the_cap_does_not_say_the_cap_was_spent(self):
        for outcome in BEFORE_CAP:
            with self.subTest(outcome=outcome):
                line = self.line(outcome)
                self.assertNotIn("cap spent", line)
                self.assertEqual(line, f"⚠️ [widgets] #7 `aaaaaaa` loop stopped — sent for a "
                                       f"ruling — {outcome} · next: adjudicator delivery pending")

    def test_a_spent_cap_still_reads_as_one(self):
        self.assertEqual(self.line(CAP), "⚠️ [widgets] #7 `aaaaaaa` loop stopped — sent for a "
                                         "ruling — 3/3 verdicts, no approval · next: adjudicator "
                                         "delivery pending")


if __name__ == "__main__":
    unittest.main()
