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

    def test_agent_hold_in_the_report_is_qualified(self):
        # #1587: the report counts operator steps only for issue-fix PRs proposed in its window.
        text = section()
        self.assertIn("issue-fix PRs", text)
        self.assertIn("in the report's window", text)
        self.assertIn("is not in the report", text)
        self.assertIn("observer.weekly_report", text)

    def test_agent_hold_record_is_qualified(self):
        # #1590: a row exists only for a hold a sweep observed and the ledger accepted.
        text = section()
        self.assertIn("when the watchdog sweep observes it begin", text)
        self.assertIn("ledger write succeeds", text)
        self.assertIn("released before a sweep sees it", text)
        self.assertIn("leaves no row", text)
        self.assertNotIn("A hold on any other PR is recorded", text)

    def test_other_docs_qualify_the_agent_hold_too(self):
        for name in ("accounts.md", "configuration.md"):
            text = " ".join((ROOT / "docs" / name).read_text().split())
            self.assertIn("issue-fix PRs proposed in the report's window"
                          if name == "configuration.md"
                          else "issue-fix PR proposed in the report's window", text, name)

    def test_other_docs_qualify_when_the_hold_is_recorded(self):
        # #1597: accounts.md and configuration.md say what operations.md says about the row.
        for name in ("accounts.md", "configuration.md"):
            text = " ".join((ROOT / "docs" / name).read_text().split())
            for phrase in ("observed it begin and the ledger write succeeded, both after the proposal",
                           "when the row was written with the proposal, not when the hold began",
                           "released before a sweep sees it", "leaves no row"):
                self.assertIn(phrase, text, name)
            self.assertNotIn("hold began after the proposal", text, name)


if __name__ == "__main__":
    unittest.main()
