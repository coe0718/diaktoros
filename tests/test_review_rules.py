"""Prompt rules from post-merge reviews (#512) and the broker's `Not verified:` check."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import broker, prompts  # noqa: E402
from test_reviewer_verdict_only import VerdictOnlyBrokerTests as Base, HEAD  # noqa: E402
from diaktoros import broker_ipc  # noqa: E402


def flat(text: str) -> str:
    return " ".join(text.split())


class FixerRules(unittest.TestCase):
    def test_issue_fix_and_fix_round_carry_rules_1_to_3(self):
        for name in ("ISOLATED_ISSUE_FIX", "ISOLATED_FIXER"):
            text = flat(getattr(prompts, name))
            with self.subTest(prompt=name):
                self.assertIn("**Reuse before writing.**", text)
                self.assertIn("call it or extend it instead of copying it", text)
                self.assertIn("**Test the enforcing entry point.**", text)
                self.assertIn("Removing the call at the entry point must turn a test red", text)
                self.assertIn("**Say what can't fire.**", text)
                self.assertIn("Don't ship it as if it works everywhere", text)


class ReviewerRule(unittest.TestCase):
    def test_reviewer_prompt_demands_the_line(self):
        text = flat(prompts.ISOLATED_REVIEWER)
        self.assertIn("`Not verified:` line", text)
        self.assertIn("Not verified: nothing", text)
        self.assertIn("The broker refuses a review without the line", text)

    def test_line_detection(self):
        ok = ("Not verified: nothing", "x\nNot verified: didn't run the sweep\n",
              "**Not verified:** the broker path", "- Not verified: x")
        for body in ok:
            with self.subTest(body=body):
                self.assertTrue(broker.has_not_verified(body))
        for body in ("", "reviewed", "Not verified:", "Not verified:\n", "I Not verified: x"):
            with self.subTest(body=body):
                self.assertFalse(broker.has_not_verified(body))


class BrokerEntryPoint(Base):
    """Drives the RunBroker `review` operation, the enforcing entry point."""

    def test_review_without_line_refused_then_resubmitted_with_it(self):
        server = self.start(broker_ipc.RunScope(broker_ipc.__dict__.get("REPO", "acme/widgets"),
                                                7, HEAD, "reviewer", "fix-7"))
        refused = self.send(server, "APPROVE", body="looks fine")
        self.assertFalse(refused["ok"])
        self.assertIn("Not verified:", refused["error"])
        self.assertEqual(self.posts, [])
        self.assertFalse(server.completed)
        ok = self.send(server, "APPROVE", body="looks fine\nNot verified: nothing")
        self.assertTrue(ok["ok"])
        self.assertEqual(len(self.posts), 1)


# Don't re-run the inherited tests under this class.
for _name in [n for n in dir(Base) if n.startswith("test_")]:
    setattr(BrokerEntryPoint, _name, None)

if __name__ == "__main__":
    unittest.main()
