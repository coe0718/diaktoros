"""#2248: a fix turn a maintainer's reply woke settles before it starts.

A maintainer comments "Fixed on main … Closing." and closes the issue seconds later. The comment
woke the fixer at once, the close landed mid-turn, and the turn spent the model's tokens to end
denied, with a notice to the operator. Now a reply-woken turn (the webhook, the sweep's reply
wake, an answered ask's resume) is queued ``reply_settle_seconds`` ahead; at its due time the
pre-launch read ends a closed issue's turn before any model runs, logged and never noticed. A
write denied because the issue closed is logged, not noticed; any other denial still is.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_closed_asks as asks  # noqa: E402
import test_fix_reply as replies  # noqa: E402
import test_issue_fix as base  # noqa: E402
BASE, REPO, issue = base.BASE, base.REPO, base.issue

from diaktoros import broker, config, gh, observer, run_supervisor  # noqa: E402
from diaktoros import seat_model, trusted_turn  # noqa: E402
from diaktoros.run_supervisor import Supervisor, issue_inputs  # noqa: E402
from diaktoros.state import ledger  # noqa: E402


def load_tests(loader, standard_tests, pattern):
    """This file's own tests only: the fixtures' classes are subclassed for their setUp, and
    their tests already run in their own files."""
    suite = unittest.TestSuite()
    for case in (ReplyWakes, AskResume, SettledRow, DueTurn, QuietDenial):
        names = sorted(n for n in vars(case) if n.startswith("test_"))
        suite.addTests(case(name) for name in names)
    return suite


class ReplyWakes(replies.Delivery):
    """The webhook and the sweep's reply wake ask for the window; a fix label never does."""

    def test_a_reply_webhook_queues_the_turn_to_start_after_the_window(self):
        self.reply(201, "jeremy")
        decision, reason = self.deliver(self.comment())
        self.assertEqual(decision, "accepted", reason)
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])
        self.assertEqual(self.settles, [120])                    # not runnable now
        (args, kwargs), = self.notices
        self.assertIn("starts in 120s unless the issue closes", kwargs["outcome"])

    def test_the_sweeps_reply_wake_settles_too(self):
        self.reply(201, "jeremy")
        self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:reply201")])
        self.assertEqual(self.settles, [120])

    def test_zero_starts_a_replys_turn_at_once_as_before(self):
        self.loop = {**config.normalize({**self.raw, "reply_settle_seconds": 0}),
                     "comment_webhook": True}
        self.reply(201, "jeremy")
        self.deliver(self.comment())
        self.assertEqual((self.enqueued, self.settles),
                         ([("issue_fixer", 12, BASE, "issue-fix:reply201")], [0]))
        (args, kwargs), = self.notices
        self.assertNotIn("starts in", kwargs["outcome"])

    def test_a_fix_labels_turn_starts_at_once_whatever_the_window(self):
        self.loop = config.normalize({**self.raw, "reply_settle_seconds": 600})
        self.run_gate(self.labeled())
        self.assertEqual((self.enqueued, self.settles),
                         ([("issue_fixer", 12, BASE, "issue-fix")], [0]))


class AskResume(asks.ClosedAsks):
    def test_an_answered_asks_resume_settles(self):
        self.reply()
        self.sweep()
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix:ask1")])
        self.assertEqual(self.settles, [120])


def settled_supervisor(test, db) -> Supervisor:
    """A worker-mode supervisor on ``db`` whose spawn starts nothing."""
    sup = Supervisor(db, fixture_mode=True, fixture_command=["true"])
    patch = mock.patch.object(Supervisor, "_spawn")
    patch.start()
    test.addCleanup(patch.stop)
    return sup


def row(db, delivery="d") -> dict:
    with ledger.connect(db) as con:
        found = con.execute("SELECT state, error, retry_at FROM runs WHERE delivery=?",
                            (delivery,)).fetchone()
    return dict(zip(("state", "error", "retry_at"), found))


def fall_due(db, delivery="d"):
    with ledger.connect(db) as con:
        con.execute("UPDATE runs SET retry_at=? WHERE delivery=?", (time.time() - 1, delivery))


class SettledRow(base.Base):
    """The ledger's side: a settled row waits on ``SETTLE_HOLD`` and becomes pending when due."""

    def test_a_settled_turn_waits_out_its_window_then_is_pending(self):
        sup = settled_supervisor(self, self.db)
        now = time.time()
        self.assertEqual(sup.submit("d", REPO, 12, BASE, "issue_fixer", turn_key="issue-fix:reply1",
                                    settle=120), "enqueued")
        held = row(self.db)
        self.assertEqual((held["state"], held["error"]), ("waiting", run_supervisor.SETTLE_HOLD))
        self.assertAlmostEqual(held["retry_at"], now + 120, delta=5)
        self.assertIsNone(sup._claim())                          # nothing runnable now
        sup.recover()
        self.assertEqual(row(self.db)["state"], "waiting")
        fall_due(self.db)
        sup.recover()
        self.assertEqual(row(self.db)["state"], "pending")
        self.assertIsNotNone(sup._claim())

    def test_no_window_is_pending_at_once(self):
        sup = settled_supervisor(self, self.db)
        sup.submit("d", REPO, 12, BASE, "issue_fixer", turn_key="issue-fix:reply1", settle=0)
        self.assertEqual(row(self.db)["state"], "pending")

    def test_a_worker_lingers_for_a_settling_turn_and_starts_it_when_due(self):
        sup = settled_supervisor(self, self.db)
        sup.submit("d", REPO, 12, BASE, "issue_fixer", turn_key="issue-fix:reply1", settle=30)
        slept = []
        sup.linger_for_ci(sleep=lambda s: slept.append(s) or fall_due(self.db))
        self.assertEqual((len(slept), row(self.db)["state"]), (1, "pending"))


class DueTurn(base.Base):
    """At its due time the real claim and ``_run_production`` decide the turn, outside faked."""

    def setUp(self):
        super().setUp()
        self.live, self.launched, self.notices = issue(), [], []
        sup = self.sup = settled_supervisor(self, self.db)
        sup.submit("d", REPO, 12, BASE, "issue_fixer", turn_key="issue-fix:reply1", settle=120)
        fall_due(self.db)
        sup.recover()
        self.run_id, owner = sup._claim()
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='launching', launch_intent=? WHERE id=?",
                        (time.time(), self.run_id))
        inference = mock.Mock(upstream="https://api.example/v1/chat/completions", key="k",
                              model="m", api_mode="chat_completions", proxy_model="",
                              client_identity="", provider="custom", profile="coder")
        inference.credential_provider.return_value = None

        def run_turn(loop, scope, **kw):
            self.launched.append((scope.role, scope.branch, scope.head))
            return 0
        sup.production_config = self.root / "runtime.json"
        self.owner, self.err = owner, io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for patch in (mock.patch.object(seat_model, "load_runtime", return_value={
                          "source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}),
                      mock.patch.object(seat_model, "resolve_profile", return_value=inference),
                      mock.patch.object(config, "by_repo", return_value=self.loop),
                      mock.patch.object(gh, "api", side_effect=lambda *a, **k: self.live),
                      mock.patch.object(gh, "issue_comments_read", return_value=([], "")),
                      mock.patch.object(trusted_turn, "run_turn", side_effect=run_turn),
                      mock.patch.object(observer, "notify",
                                        side_effect=lambda *a, **k: self.notices.append(a)),
                      mock.patch.object(sup, "recover"),
                      mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)})):
            stack.enter_context(patch)

    def go(self) -> str:
        """The due turn's production run; what it logged."""
        with contextlib.redirect_stderr(self.err):
            self.sup._run_production(self.run_id, self.owner)
        return self.err.getvalue()

    def test_an_issue_closed_in_the_window_ends_with_no_launch_and_no_notice(self):
        self.live = issue(state="closed")
        logged = self.go()
        ended = row(self.db)
        self.assertEqual((ended["state"], ended["error"]), ("cancelled", issue_inputs.ISSUE_CLOSED))
        self.assertEqual((self.launched, self.notices), ([], []))
        lines = [line for line in logged.splitlines() if "issue #12" in line]
        self.assertEqual(len(lines), 1, logged)
        self.assertIn("fix turn ended before launch, the issue was closed", lines[0])

    def test_an_issue_still_open_at_its_due_time_launches_as_today(self):
        self.go()
        self.assertEqual(self.launched, [("issue_fixer", "diaktoros/issue-12", BASE)])


class QuietDenial(base.BrokerWrite):
    """The broker's 📦 notice: a close is logged, any other denial is still announced."""

    def setUp(self):
        super().setUp()
        self.notices = []
        patch = mock.patch.object(observer, "notify",
                                  side_effect=lambda *a, **k: self.notices.append((a, k)))
        patch.start()
        self.addCleanup(patch.stop)

    def deny(self) -> str:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(broker.BrokerDenied):
            self.server._dispatch(json.dumps({"operation": "issue_comment",
                                              "body": "x"}).encode())
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "denied")
        return err.getvalue()

    def test_a_write_denied_because_the_issue_closed_is_logged_not_noticed(self):
        self.live = issue(state="closed")
        logged = self.deny()
        self.assertEqual(self.notices, [])
        self.assertIn("issue #12: fix turn's write denied, the issue was closed", logged)

    def test_another_denial_is_still_noticed(self):
        self.live = issue(labels=())
        self.deny()
        (args, kwargs), = self.notices
        self.assertEqual(args[2], "fixed")
        self.assertIn("denied: no longer eligible: the 'agent-fix' label", kwargs["outcome"])


if __name__ == "__main__":
    unittest.main()
