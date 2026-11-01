"""The holding-one-PR section must name operator agents as holders (#1574).

``config.maintainers`` is ``triage.maintainers`` united with ``operator_agents`` (#757), and
``pr_hold`` honours that union, so a loop with only operator agents can hold.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def section() -> str:
    text = (ROOT / "docs" / "operations.md").read_text()
    body = text.split("## Holding one PR", 1)[1].split("\n## ", 1)[0]
    return " ".join(body.split())


class HoldingSectionNamesOperatorAgents(unittest.TestCase):
    def test_operator_agents_may_hold(self):
        self.assertIn("operator_agents", section())

    def test_cannot_fire_only_without_both(self):
        text = section()
        self.assertNotIn("cannot fire on a loop without `triage.maintainers`", text)
        self.assertIn("neither `triage.maintainers`", text)
        self.assertIn("nor `operator_agents`", text)


if __name__ == "__main__":
    unittest.main()
