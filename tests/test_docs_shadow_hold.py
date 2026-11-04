"""#1803: the shadow-hold text must agree with ``gate.shadow_state`` (#1683).

A head with a single reviewer turn is not held when its shadow ended without a review; a head
with an earlier turn's shadow is. The only escapes at the second head are ``shadow.escalate``
off or a new head: a second ``review --reconsider`` is refused and a plain ``review`` queues no
new turn. The docs and the function's docstring must not say otherwise.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest
from pathlib import Path

from diaktoros import gate

ROOT = Path(__file__).resolve().parents[1]


def paragraph() -> str:
    text = (ROOT / "docs" / "configuration.md").read_text()
    body = text.split("**Holding the PR (`shadow.escalate`, #1004).**", 1)[1].split("\n\n", 1)[0]
    return " ".join(body.split())


class ShadowHoldDocs(unittest.TestCase):
    def test_never_held_is_qualified_to_a_single_reviewer_turn(self):
        text = paragraph()
        self.assertNotIn("is never held", text)
        self.assertIn("not held when it has a single reviewer turn", text)

    def test_only_escapes_that_exist_at_a_reconsidered_head_are_named(self):
        text = paragraph()
        self.assertNotIn("until a person re-reviews", text)
        self.assertIn("`set --shadow-escalate off`", text)
        self.assertIn("a new head", text)
        self.assertIn("Another `review --reconsider` is refused", text)

    def test_docstring_does_not_claim_a_failed_shadow_never_holds(self):
        doc = " ".join(gate.shadow_state.__doc__.split())
        self.assertNotIn("never parks a PR for good", doc)
        self.assertIn("earlier turn's shadow", doc)


if __name__ == "__main__":
    unittest.main()
