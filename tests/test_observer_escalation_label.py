"""The ``escalation`` notice's label is true for every breach the gate reports (#2460).

``gate.breach`` sends one ``escalation`` event for a spent cap and for an escalation raised before
it (the fixer's dispute, the shadow review, the cycle and the operator). The label comes from the
event alone, so it must not claim the cap was spent; the outcome clause says why it stopped.

The notice is sent from the breach reservation, before the adjudicator is woken (#2558 review
F1), so it must not say the PR was sent for a ruling either: with no adjudicator route nothing is
woken, and a failed delivery stays pending."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from diaktoros import gate, observer  # noqa: E402

LOOP = {"id": "widgets", "repo": "o/widgets", "cap": 3}
HEAD = "a" * 40
LABEL = "loop stopped — a ruling is needed"
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
                self.assertEqual(line, f"⚠️ [widgets] #7 `aaaaaaa` {LABEL} — {outcome} · "
                                       "next: adjudicator delivery pending")

    def test_a_spent_cap_still_reads_as_one(self):
        self.assertEqual(self.line(CAP), f"⚠️ [widgets] #7 `aaaaaaa` {LABEL} — 3/3 verdicts, "
                                         "no approval · next: adjudicator delivery pending")


class _State:
    """The breach marker store, reduced to ``breach_deliver``'s order: reserve (the notice),
    then send. ``sent`` records what delivery returned."""

    def __init__(self):
        self.sent = []

    def breach_deliver(self, number, entry, current, send, reserved=None):
        marker = {**entry, "status": "delivery-pending"}
        if reserved:
            reserved(marker)
        self.sent.append(send(marker))
        return "new"

    def note(self, _text):
        pass


class UndeliveredBreach(unittest.TestCase):
    """The real ``gate.breach`` notice when nothing reached the adjudicator (#2558 review F1)."""

    def breach(self, route):
        loop = {**LOOP, "base": "main", "fixers": ["bot"],
                "adjudicator": {"route": route} if route else {}}
        st, lines = _State(), []

        def notify(loop_, _st, event, number, head, **kw):
            lines.append(observer.summarize(loop_, event, number, head, outcome=kw["outcome"],
                                            next_turn=kw["next_turn"]))

        with mock.patch.object(gate.observer, "notify", side_effect=notify), \
                mock.patch.object(gate, "wake_adjudicator", return_value=False) as wake:
            gate.breach(loop, st, 7, HEAD, 3, "cap")
        return lines, st.sent, wake.call_count

    def test_no_adjudicator_route_does_not_say_it_was_sent(self):
        lines, _sent, woken = self.breach(None)
        self.assertEqual(woken, 0)
        self.assertEqual(lines, [f"⚠️ [widgets] #7 `aaaaaaa` {LABEL} — {CAP} · next: you"])

    def test_a_failed_delivery_does_not_say_it_was_sent(self):
        lines, sent, woken = self.breach("arbiter")
        self.assertEqual((woken, sent), (1, [False]))
        self.assertEqual(lines, [f"⚠️ [widgets] #7 `aaaaaaa` {LABEL} — {CAP} · "
                                 "next: adjudicator delivery pending"])
        self.assertNotIn("sent for", lines[0])


if __name__ == "__main__":
    unittest.main()
