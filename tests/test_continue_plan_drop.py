"""`continue --post-plan`: a stranded plan reservation is dropped, and the way back is named
(#2367, #2370).

An uncertain plan-comment POST leaves the run's plan ask ``pending``. When a conflict then
stands (the pushed branch moved, the issue's comments are unreadable), ``--post-plan`` drops
that reservation outright instead of adopting it, so the issue is not held on an ask whose run
cannot open its PR, and its line names the recovery: inspect with ``--dry-run``, then post a
fresh plan. ``status`` and ``explain`` name the ``--post-plan`` command whole, with the
``--loop`` the CLI requires.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diaktoros import broker, config, fix_hold, gate, gh  # noqa: E402
from diaktoros.cli import operations  # noqa: E402
import test_continue_run as tcr  # noqa: E402
from test_issue_fix import issue  # noqa: E402


class StrandedPlan(tcr.Continue):
    def labelled(self):
        return issue(labels=("agent-fix", fix_hold.PLAN_LABEL))

    def post_plan_said(self, live, comments, error="", post=None):
        """``--post-plan`` through the CLI: ``(exit code, what it printed)``. ``post`` replaces
        the comment POST (default: it lands as comment 500)."""
        args = argparse.Namespace(loop="widgets", run=self.run_id, dry_run=False, post_plan=True)
        out = io.StringIO()
        with mock.patch.object(gh, "fetch", self.fetch_with(live)), self.real_policy(live), \
                mock.patch.object(gh, "issue_comments_read", lambda loop, n: (comments, error)), \
                mock.patch.object(broker, "post_issue_comment", post or self.plan_comment), \
                mock.patch.object(config, "load_id", return_value=self.loop), \
                mock.patch.object(operations, "_ledger_path", return_value=self.db), \
                contextlib.redirect_stdout(out):
            code = operations.cmd_continue(args)
        return code, out.getvalue()

    def test_branch_moved_drops_a_landed_reservation_and_names_the_recovery(self):
        """The lost POST did land, then the branch moves: the reservation is dropped (still
        counted, its comment is on the issue), not adopted; the seat may ask again, and the
        output names the dry run and a fresh `--post-plan`."""
        st = self.lost_plan(self.labelled())
        self.branch_sha = "e" * 40
        on_issue = [{"id": 500, "user": {"login": "fix"}, "body": self.posted}]
        code, said = self.post_plan_said(self.labelled(), on_issue)
        self.assertEqual(code, 2)
        self.assertEqual((st.fix_ask_open(), st.fix_ask_count(12)), ({}, 1))
        fix_hold.check_ask(st, 12)
        self.assertIn(f"dropped run {self.run_id}'s unconfirmed plan reservation on issue #12", said)
        self.assertIn("plan comment 500 is on the issue", said)
        self.assertIn(f"`hermes dk continue --loop widgets --run {self.run_id} --dry-run`", said)
        self.assertIn("run `--post-plan` again to post a fresh plan", said)
        self.assertEqual(self.writes, ["plan_comment"])

    def test_branch_moved_with_unreadable_comments_still_drops(self):
        """The branch moved and the issue's comments cannot be read: the reservation is not
        left pending for a retry that may never pass; it is dropped, counted (its comment may
        be on the issue)."""
        st = self.lost_plan(self.labelled())
        self.branch_sha = "e" * 40
        code, said = self.post_plan_said(self.labelled(), None, error="HTTP 502")
        self.assertEqual(code, 2)
        self.assertEqual((st.fix_ask_open(), st.fix_ask_count(12)), ({}, 1))
        fix_hold.check_ask(st, 12)
        self.assertIn("may be on the issue: comments unreadable (HTTP 502)", said)
        self.assertIn("--dry-run", said)

    def test_after_the_drop_a_fresh_plan_can_be_posted(self):
        """The recovery the line names works: once the branch is back where the ledger has it,
        `--post-plan` posts a fresh plan and opens a new, confirmed ask."""
        st = self.lost_plan(self.labelled())
        self.branch_sha = "e" * 40
        self.assertEqual(self.post_plan_said(self.labelled(), [])[0], 2)
        self.assertEqual(st.fix_ask_open(), {})
        self.branch_sha = tcr.NEW
        code, _ = self.post_plan_said(self.labelled(), [])
        self.assertEqual(code, 0)
        self.assertEqual(self.writes, ["plan_comment", "plan_comment"])
        self.assertEqual(st.fix_ask_open()[12]["comment_id"], 500)
        self.assertNotIn("pending", st.fix_ask_open()[12])

    def test_a_fresh_plan_never_adopts_the_dropped_plans_comment(self):
        """Review on #2579, F1/F2: the old plan landed (500) and a maintainer approved it (501),
        then the branch moved and the reservation was dropped. Back on the ledger's head, a fresh
        `--post-plan` loses its POST's response too, though it landed (502). The retry adopts
        502, its own, not 500; and the approval before 502 does not approve the fresh plan."""
        st = self.lost_plan(self.labelled())
        old = {"id": 500, "user": {"login": "fix"}, "body": self.posted}
        approve = {"id": 501, "user": {"login": "Jeremy"}, "body": fix_hold.APPROVE}
        self.branch_sha = "e" * 40
        self.assertEqual(self.post_plan_said(self.labelled(), [old, approve])[0], 2)
        self.assertEqual(st.fix_ask_open(), {})
        self.branch_sha = tcr.NEW

        def lost(loop, **kw):
            self.writes.append("plan_comment")
            self.posted = kw["body"]
            raise broker.BrokerDenied("GitHub comment write did not return a successful response")
        self.assertEqual(self.post_plan_said(self.labelled(), [old, approve], post=lost)[0], 2)
        self.assertTrue(st.fix_ask_open()[12]["pending"])
        self.assertNotEqual(self.posted, old["body"])          # its own marker, not the old one
        fresh = {"id": 502, "user": {"login": "fix"}, "body": self.posted}
        on_issue = [old, approve, fresh]
        code, said = self.post_plan_said(self.labelled(), on_issue)
        self.assertEqual(code, 0, said)
        self.assertEqual(self.writes, ["plan_comment"] * 2)    # adopted, not posted a third time
        self.assertEqual(st.fix_ask_open()[12]["comment_id"], 502)
        with mock.patch.object(gh, "issue_comments_read", lambda loop, n: (on_issue, "")), \
                mock.patch.object(gate, "enqueue_isolated", lambda *a, **k: None):
            fix_hold.sweep_asks(self.loop, st, [])
        self.assertFalse(st.fix_plan_approved(12))             # 501 approved the dropped plan
        on_issue.append({**approve, "id": 503})                # control: one after 502 does
        with mock.patch.object(gh, "issue_comments_read", lambda loop, n: (on_issue, "")), \
                mock.patch.object(gate, "enqueue_isolated", lambda *a, **k: None):
            fix_hold.sweep_asks(self.loop, st, [])
        self.assertTrue(st.fix_plan_approved(12))

    def test_status_and_explain_name_the_whole_post_plan_command(self):
        """A pending reservation's `status`/`explain` line names a command the CLI accepts:
        `--loop` is required, so it is named."""
        st = self.lost_plan(self.labelled())
        want = f"`hermes dk continue --loop widgets --run {self.run_id} --post-plan` again"
        self.assertIn(want, "\n".join(fix_hold.status_lines(self.loop, st)))
        self.assertIn(want, "\n".join(line for _, line in
                                      fix_hold.explain_rows(self.loop, st, 12)))


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the borrowed harness's run in test_continue_run."""
    suite = unittest.TestSuite()
    for name in sorted(vars(StrandedPlan)):
        if name.startswith("test_"):
            suite.addTest(StrandedPlan(name))
    return suite


if __name__ == "__main__":
    unittest.main()
