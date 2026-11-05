"""#757 / #1339: the behaviour around ``operator_agents`` that lives in code which existed before
the authorship check — an agent's comment reaching the seats (``issue_facts``), an agent's hold
(``pr_hold.decide``, ``explain``), and the lines ``cmd_explain`` prints from a report.

These tests import only modules that existed before ``diaktoros.authorship`` was added, so a
fail-then-pass proof of a fix to this behaviour runs them on the original head instead of erroring
the whole module on an import (#1339). The tests of ``authorship`` itself stay in
``test_authorship.py``. GitHub is mocked; nothing real is touched."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import config, explain, gh, issue_facts, pr_hold  # noqa: E402
from diaktoros import state as state_mod  # noqa: E402
from diaktoros.cli import operations  # noqa: E402


class AgentComments(fg.Base):
    """The comment's route to the seats, and the label on it."""

    def comments(self, loop, number):
        return [{"user": {"login": "Docs-Bot"}, "body": "AGENT-SAYS", "created_at": "t"},
                {"user": {"login": "boss"}, "body": "BOSS-SAYS", "created_at": "t"},
                {"user": {"login": "stranger"}, "body": "STRANGER-SAYS", "created_at": "t"}], ""

    def loop_with(self, agents):
        loop = config.normalize({**fg.raw_loop(False), "state_dir": str(self.root / "state"),
                                 "operator_agents": agents})
        loop["triage"] = {"maintainers": ["boss"]}      # as test_issue_facts: no triage seat needed
        return loop

    def test_an_agents_comment_reaches_the_seats_labelled_and_a_strangers_does_not(self):
        loop = self.loop_with(["docs-bot"])
        with mock.patch.object(gh, "issue_comments_read", side_effect=self.comments):
            text = issue_facts.comments_section(loop, 5)
        self.assertIn("AGENT-SAYS", text)
        self.assertIn("### docs-bot (operator agent, t)", text)
        self.assertIn("### boss (t)", text)               # the person is not labelled an agent
        self.assertNotIn("STRANGER-SAYS", text)

    def test_without_the_setting_the_agents_comment_is_left_out(self):
        loop = self.loop_with([])
        with mock.patch.object(gh, "issue_comments_read", side_effect=self.comments):
            text = issue_facts.comments_section(loop, 5)
        self.assertNotIn("AGENT-SAYS", text)
        self.assertNotIn("operator agent", text)

    def test_an_agents_hold_counts_and_explain_names_it(self):
        loop = self.loop_with(["docs-bot"])
        events = [{"event": "labeled", "actor": {"login": "docs-bot"},
                   "label": {"name": pr_hold.LABEL}, "created_at": "2026-01-01T00:00:00Z"}]
        hold = pr_hold.decide(loop, 7, events, [])
        self.assertEqual(hold["by"], "docs-bot")
        facts = {"pr": fg.LIVE, "reviews": [], "armed": True, "read_at": 0, "hold": hold}
        report = explain.explain(loop, state_mod.state_for(loop), 7, facts)
        self.assertTrue(any("held by docs-bot (operator agent)" in b for b in report["blockers"]))
        self.assertEqual(report["operator_agents"], ["docs-bot"])


class CmdExplainPrinting(fg.Base):
    """``cmd_explain`` prints the report's ``authorship`` and ``operator_agents`` entries. The
    report is built here, not by ``authorship``, so this needs nothing the fix added."""

    STRAY_LINE = ("misattributed: commit ccccccc shows as fixer's, but no seat turn pushed it")

    def printed(self, **extra):
        loop = config.normalize({**fg.raw_loop(False), "state_dir": str(self.root / "state")})
        facts = {"pr": fg.LIVE, "reviews": [], "armed": True, "read_at": 0}
        report = {**explain.explain(loop, state_mod.state_for(loop), 7, facts), **extra}
        args = mock.Mock(loop="one", pr=7)
        out = io.StringIO()
        with mock.patch.object(explain, "explain", return_value=report), \
             mock.patch.object(explain, "explain_facts", return_value={}), \
             mock.patch.object(operations, "_print_ledger_runs", return_value=[]), \
             mock.patch.object(operations, "_print_shadows"), \
             mock.patch.object(operations, "_view_lines", return_value=[]), \
             mock.patch.object(operations, "_dependency_lines", return_value=[]), \
             contextlib.redirect_stdout(out):
            operations.cmd_explain(args)
        return out.getvalue()

    def test_cmd_explain_prints_the_authorship_line(self):
        text = self.printed(authorship=[self.STRAY_LINE])
        self.assertRegex(text, r"authorship:\s+misattributed: commit ccccccc")

    def test_cmd_explain_prints_no_authorship_line_when_all_is_well(self):
        self.assertNotIn("authorship:", self.printed(authorship=[]))

    def test_cmd_explain_names_the_operator_agents(self):
        text = self.printed(operator_agents=["docs-bot"])
        self.assertRegex(text, r"operators:\s+docs-bot post as the operator's account")
        self.assertNotIn("operators:", self.printed(operator_agents=[]))


if __name__ == "__main__":
    unittest.main()
