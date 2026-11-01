"""#905: the watchdog sweep acts on an ACCEPT ruling (``ruled_approval.sweep``).

With ``adjudicator_approves`` on and the adjudicator's login set, an ACCEPT at a PR's current head
over the reviewer's change request is acted on once the required checks are green, no
``human_paths`` file is touched and no maintainer holds the PR: the adjudicator approves the head,
then dismisses the change request at it. Each write is recorded before it is sent and never
replayed; a wait is checked again next sweep; a change request at another head is left to a
person. GitHub, the CI read and the broker's writes are mocked; the ledger is real.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
import test_ruled_approval as tra  # noqa: E402
from diaktoros import broker, ci, config, merge_check, pr_hold, ruled_approval  # noqa: E402
from diaktoros import run_supervisor  # noqa: E402

PR = {**fg.LIVE, "user": {"login": "fixer"}}
CHANGES, DISMISSED, TUCK = tra.CHANGES, tra.DISMISSED, tra.TUCK


class Base(tra.Base):
    def setUp(self):
        super().setUp()
        self.loop = {**self.loop, "adjudicator_approves": True}
        self.reviews = [CHANGES]
        self.ci = ci.CIState(passed=["tests"])
        self.paths = ("", "")
        self.hold = (None, "")
        self.approve = mock.Mock(side_effect=self._approved)
        self.dismiss = mock.Mock(side_effect=self._dismissed)
        self.logged = []
        for target, name, kw in (
                (ruled_approval.gh, "reviews", {"side_effect": lambda *_a, **_k: list(self.reviews)}),
                (ruled_approval.ci, "read", {"side_effect": lambda *_a: self.ci}),
                (broker, "human_paths_check", {"side_effect": lambda *_a: self.paths}),
                (pr_hold, "read", {"side_effect": lambda *_a: self.hold}),
                (broker, "authorize_adjudicator", {"return_value": "adj"}),
                (broker, "approve_on_ruling", {"new": self.approve}),
                (broker, "dismiss_on_ruling", {"new": self.dismiss}),
                (merge_check, "refresh_quietly", {})):
            patch = mock.patch.object(target, name, **kw)
            patch.start()
            self.addCleanup(patch.stop)

    def _approved(self, *_a, **_kw):
        self.reviews = [*self.reviews, TUCK]
        return TUCK["id"]

    def _dismissed(self, *_a, review_id, **_kw):
        self.reviews = [DISMISSED if r["id"] == review_id else r for r in self.reviews]

    def sweep(self):
        return ruled_approval.sweep(self.loop, self.st, [PR], self.logged.append)

    def row(self):
        return run_supervisor.Supervisor(self.db).ruled_approval(fg.REPO, 7, fg.HEAD)


class Acts(Base):
    def test_an_accept_with_green_checks_approves_then_dismisses(self):
        self.ruled("ACCEPT")
        self.assertEqual(self.sweep(), [7])
        self.approve.assert_called_once()
        self.assertEqual(self.approve.call_args.kwargs["head"], fg.HEAD)
        self.assertIn("did not re-review", self.approve.call_args.kwargs["text"])
        self.dismiss.assert_called_once()
        self.assertEqual(self.dismiss.call_args.kwargs["review_id"], CHANGES["id"])
        row = self.row()
        self.assertEqual((row["approve"], row["review_id"], row["dismiss"], row["dismissed"]),
                         ("posted", TUCK["id"], "posted", CHANGES["id"]))
        merge_check.refresh_quietly.assert_called_once_with(self.loop, 7)
        # Done: a second sweep writes nothing again.
        self.assertEqual(self.sweep(), [])
        self.approve.assert_called_once()
        self.dismiss.assert_called_once()

    def test_an_approval_already_counted_is_only_dismissed_for(self):
        self.ruled("ACCEPT")
        self.reviews = [CHANGES, TUCK]                     # approved by hand, or an earlier sweep
        self.sweep()
        self.approve.assert_not_called()
        self.dismiss.assert_called_once()
        self.assertEqual(self.row()["dismiss"], "posted")

    def test_nothing_left_to_dismiss_is_recorded_as_none(self):
        self.ruled("ACCEPT")
        self.reviews = [DISMISSED, TUCK]
        self.sweep()
        self.dismiss.assert_not_called()
        self.assertEqual(self.row()["dismiss"], "none")


class DoesNothing(Base):
    def assert_untouched(self):
        self.assertEqual(self.sweep(), [])
        self.approve.assert_not_called()
        self.dismiss.assert_not_called()
        self.assertIsNone(self.row())

    def test_with_the_setting_off(self):
        self.ruled("ACCEPT")
        self.loop = {**self.loop, "adjudicator_approves": False}
        self.assert_untouched()

    def test_without_the_adjudicator_login(self):
        self.ruled("ACCEPT")
        self.loop = {**self.loop, "seats": {k: v for k, v in self.loop["seats"].items()
                                            if k != "adjudicator"}}
        self.assertFalse(config.adjudicator_approves(self.loop))
        self.assert_untouched()

    def test_without_an_accept_at_the_current_head(self):
        self.assert_untouched()
        self.ruled("ACCEPT", head="b" * 40)
        self.assert_untouched()
        self.ruled("ACCEPT")
        self.ruled("REJECT")
        self.assert_untouched()

    def test_when_the_reviewer_approved(self):
        # A shadow escalation's ACCEPT: the reviewer's approval stands, nothing to overrule.
        self.ruled("ACCEPT")
        self.reviews = [tra.review(5, "APPROVED")]
        self.assert_untouched()

    def test_for_a_draft_or_a_strangers_pr(self):
        self.ruled("ACCEPT")
        for pr in ({**PR, "draft": True}, {**PR, "user": {"login": "stranger"}}):
            with self.subTest(pr=pr):
                self.assertEqual(ruled_approval.sweep(self.loop, self.st, [pr]), [])
        self.approve.assert_not_called()


class Waits(Base):
    def test_red_running_or_unread_checks_wait_then_it_acts(self):
        self.ruled("ACCEPT")
        for state in (ci.CIState(failed=["tests"]), ci.CIState(pending=["tests"]), None):
            with self.subTest(state=state):
                self.ci = state
                self.assertEqual(self.sweep(), [])
                self.assertIsNone(self.row())
        self.approve.assert_not_called()
        self.ci = ci.CIState(passed=["tests"])
        self.assertEqual(self.sweep(), [7])
        self.approve.assert_called_once()

    def test_a_maintainer_hold_or_an_unreadable_one_waits(self):
        self.ruled("ACCEPT")
        for hold in (({"by": "coe0718"}, ""), (None, "HTTP 502")):
            with self.subTest(hold=hold):
                self.hold = hold
                self.assertEqual(self.sweep(), [])
        self.assertIsNone(self.row())
        self.approve.assert_not_called()

    def test_an_unreadable_file_list_waits(self):
        self.ruled("ACCEPT")
        self.paths = ("the PR's file list could not be read", "")
        self.assertEqual(self.sweep(), [])
        self.assertIsNone(self.row())


class LeftToAPerson(Base):
    def test_a_human_paths_diff_is_denied(self):
        self.ruled("ACCEPT")
        self.paths = ("", "it touches .github/workflows/ci.yml")
        self.assertEqual(self.sweep(), [7])
        self.approve.assert_not_called()
        row = self.row()
        self.assertEqual(row["approve"], "denied")
        self.assertIn("human_paths", row["error"])
        self.sweep()
        self.approve.assert_not_called()

    def test_a_human_hold_is_denied(self):
        self.ruled("ACCEPT")
        self.st.human_hold_set(7, fg.HEAD, "touches human_paths")
        self.sweep()
        self.approve.assert_not_called()
        self.assertEqual(self.row()["approve"], "denied")

    def test_a_refused_identity_is_denied(self):
        self.ruled("ACCEPT")
        with mock.patch.object(broker, "authorize_adjudicator",
                               side_effect=broker.BrokerDenied("stale PR head")):
            self.sweep()
        self.approve.assert_not_called()
        self.assertEqual((self.row()["approve"], self.row()["error"]), ("denied", "stale PR head"))

    def test_a_lost_approval_is_uncertain_and_never_replayed(self):
        self.ruled("ACCEPT")
        self.approve.side_effect = TimeoutError("read timed out")
        self.sweep()
        self.assertEqual(self.row()["approve"], "uncertain")
        self.sweep()
        self.approve.assert_called_once()
        self.dismiss.assert_not_called()

    def test_a_lost_dismissal_is_uncertain_and_never_replayed(self):
        self.ruled("ACCEPT")
        self.dismiss.side_effect = TimeoutError("read timed out")
        self.sweep()
        self.assertEqual((self.row()["approve"], self.row()["dismiss"]), ("posted", "uncertain"))
        self.sweep()
        self.dismiss.assert_called_once()

    def test_a_change_request_at_another_head_is_never_dismissed(self):
        self.ruled("ACCEPT")
        older = tra.review(3, "CHANGES_REQUESTED", login="critic", head="e" * 40)
        self.loop = {**self.loop, "reviewers": ["reviewer", "critic"]}
        self.reviews = [older, CHANGES]
        self.sweep()
        self.assertEqual([c.kwargs["review_id"] for c in self.dismiss.call_args_list], [5])
        self.assertIn("review 3 at another head still blocks", self.logged[-1])


class Steps(Base):
    def test_a_step_only_moves_forward(self):
        sup = run_supervisor.Supervisor(self.db)
        step = lambda *a, **kw: sup.ruled_approval_step(fg.REPO, 7, fg.HEAD, *a, **kw)  # noqa: E731
        self.assertFalse(step("approve", "posted"))           # not before posting
        self.assertTrue(step("approve", "posting"))
        self.assertFalse(step("approve", "posting"))          # a second sweep claims nothing
        self.assertTrue(step("approve", "posted", review_id=9))
        self.assertFalse(step("approve", "uncertain"))
        self.assertTrue(step("dismiss", "none"))
        self.assertFalse(step("dismiss", "posting"))
        with self.assertRaises(ValueError):
            step("approve", "none")
        with self.assertRaises(ValueError):
            step("merge", "posting")
        self.assertEqual(sup.ruled_approval(fg.REPO, 7, fg.HEAD)["review_id"], 9)


class BrokerWrites(unittest.TestCase):
    LOOP = {"repo": fg.REPO, "state_dir": "/nonexistent", "attribution": False}

    def test_the_approval_is_pinned_to_the_head(self):
        reply = {"id": 9, "state": "APPROVED", "commit_id": fg.HEAD}
        with mock.patch.object(broker, "_write", return_value=(reply, "")) as write, \
                mock.patch.object(broker, "_audit") as audit:
            self.assertEqual(broker.approve_on_ruling(self.LOOP, repo=fg.REPO, number=7,
                                                      head=fg.HEAD, branch="b", login="adj",
                                                      text="t"), 9)
        self.assertEqual(write.call_args.args[1], f"/repos/{fg.REPO}/pulls/7/reviews")
        body = write.call_args.kwargs["body"]
        self.assertEqual((body["event"], body["commit_id"]), ("APPROVE", fg.HEAD))
        self.assertEqual(write.call_args.kwargs["login"], "adj")
        audit.assert_called_once()
        for bad in ({"id": 9, "state": "COMMENTED", "commit_id": fg.HEAD},
                    {"id": 9, "state": "APPROVED", "commit_id": "f" * 40}, None):
            with self.subTest(bad=bad), \
                    mock.patch.object(broker, "_write", return_value=(bad, "HTTP 422")), \
                    self.assertRaises(broker.BrokerDenied):
                broker.approve_on_ruling(self.LOOP, repo=fg.REPO, number=7, head=fg.HEAD,
                                         branch="b", login="adj", text="t")

    def test_the_dismissal_must_come_back_dismissed(self):
        with mock.patch.object(broker, "_write", return_value=({"state": "DISMISSED"}, "")) as w, \
                mock.patch.object(broker, "_audit"):
            broker.dismiss_on_ruling(self.LOOP, repo=fg.REPO, number=7, head=fg.HEAD, branch="b",
                                     login="adj", review_id=5, message="m")
        self.assertEqual(w.call_args.args[1], f"/repos/{fg.REPO}/pulls/7/reviews/5/dismissals")
        self.assertEqual((w.call_args.kwargs["method"], w.call_args.kwargs["body"]["event"]),
                         ("PUT", "DISMISS"))
        with mock.patch.object(broker, "_write", return_value=({"state": "CHANGES_REQUESTED"},
                                                               "")), \
                self.assertRaises(broker.BrokerDenied):
            broker.dismiss_on_ruling(self.LOOP, repo=fg.REPO, number=7, head=fg.HEAD, branch="b",
                                     login="adj", review_id=5, message="m")


class Wiring(unittest.TestCase):
    def test_the_watchdog_sweep_runs_it(self):
        from types import SimpleNamespace
        from scripts import watchdog
        c = SimpleNamespace(loop={"id": "x"}, st=None, prs=[PR], watch={})
        with mock.patch.object(watchdog.ruled_approval, "sweep") as sweep, \
                mock.patch.object(watchdog.ci_fix, "sweep"), \
                mock.patch.object(watchdog.stale_approval, "sweep"), \
                mock.patch.object(watchdog.main_check, "sweep"), \
                mock.patch.object(watchdog.ready, "sweep"), \
                mock.patch.object(watchdog.merge_check, "refresh"), \
                mock.patch.object(watchdog.conflict_person, "prune"), \
                mock.patch.object(watchdog.escalate_pending, "prune", return_value=[]):
            watchdog._best_effort_sweeps(c)
        sweep.assert_called_once()
        self.assertEqual(sweep.call_args.args[:3], (c.loop, c.st, c.prs))


if __name__ == "__main__":
    unittest.main()
