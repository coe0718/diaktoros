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
from diaktoros import broker, ci, config, gate, merge_check, pr_hold, ruled_approval  # noqa: E402
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
    def test_an_accept_with_green_checks_approves_then_dismisses_the_overruled_review(self):
        self.ruled("ACCEPT")
        self.assertEqual(self.sweep(), [7])                # this sweep approves...
        self.approve.assert_called_once()
        self.assertEqual(self.approve.call_args.kwargs["head"], fg.HEAD)
        self.assertIn("did not re-review", self.approve.call_args.kwargs["text"])
        self.dismiss.assert_not_called()
        self.assertEqual(self.sweep(), [7])                # ...the next, all rechecked, dismisses
        self.dismiss.assert_called_once()
        self.assertEqual(self.dismiss.call_args.kwargs["review_id"], CHANGES["id"])
        row = self.row()
        self.assertEqual((row["overruled"], row["approve"], row["review_id"], row["dismiss"]),
                         (CHANGES["id"], "posted", TUCK["id"], "posted"))
        self.assertEqual(merge_check.refresh_quietly.call_count, 2)
        self.assertEqual(self.sweep(), [])                 # done: nothing is written again
        self.approve.assert_called_once()
        self.dismiss.assert_called_once()

    def test_an_approval_already_counted_is_only_dismissed_for(self):
        self.ruled("ACCEPT")
        self.reviews = [CHANGES, TUCK]                     # approved by hand
        self.sweep()
        self.assertEqual((self.row()["approve"], self.row()["overruled"]), ("none", 5))
        self.sweep()
        self.approve.assert_not_called()
        self.dismiss.assert_called_once()
        self.assertEqual(self.row()["dismiss"], "posted")

    def test_nothing_to_overrule_writes_nothing(self):
        self.ruled("ACCEPT")
        self.reviews = [DISMISSED, TUCK]
        self.sweep()
        self.dismiss.assert_not_called()
        self.assertIsNone(self.row())

    def test_both_messages_link_the_ruling_or_say_where_it_is(self):
        # Review of #1605, F4.
        self.ruled("ACCEPT")
        self.sweep()
        self.sweep()
        cited = self.n
        command = f"python -m diaktoros.run_supervisor rulings {self.db} r{cited}"
        for text in (self.approve.call_args.kwargs["text"],
                     self.dismiss.call_args.kwargs["message"]):
            self.assertIn("not posted as a comment (comment pending)", text)
            self.assertIn(f"`{command}` on the host", text)
        # Review of #1605, rounds 2 and 3, F4: the cited command prints that ruling's text, even
        # after more than the 50 a plain listing shows have been made since.
        import contextlib
        import io
        import json
        for _ in range(55):
            self.ruled("REJECT", head="c" * 40)
        out = io.StringIO()
        with mock.patch.object(sys, "argv", command.split()[2:]), contextlib.redirect_stdout(out):
            run_supervisor.main()
        (printed,) = json.loads(out.getvalue())
        self.assertEqual((printed["run_id"], printed["body"]), (f"r{cited}", "because"))
        ruling = {"comment": "posted", "comment_id": 6563270384}
        self.assertEqual(ruled_approval.cite(self.loop, 7, ruling),
                         f"https://github.com/{fg.REPO}/pull/7#issuecomment-6563270384")


class Rulings(Base):
    """Review of #1605, round 2: which ruling, and which review, the sweep acts on."""

    def rule(self, verdict, created, head=fg.HEAD):
        import sqlite3
        from contextlib import closing
        self.n += 1
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,"
                        "updated) VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"q{self.n}", fg.REPO, 7, head, "breach:1", verdict, "because", created,
                         created))

    def test_a_change_request_in_the_rulings_own_second_was_not_overruled(self):
        # F2: GitHub's second-precision stamp could be up to a second after the ruling.
        from datetime import datetime, timezone
        second = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
        self.rule("ACCEPT", second + 0.1)
        self.assertEqual(self.sweep(), [])
        self.approve.assert_not_called()
        self.assertIsNone(ruled_approval.overruled([CHANGES], self.loop, fg.HEAD, second + 0.1))
        self.assertIs(ruled_approval.overruled([CHANGES], self.loop, fg.HEAD, second + 1), CHANGES)

    def test_a_later_ruling_on_another_head_voids_this_heads_accept(self):
        # F5: the branch returns to an old head after a REJECT on a newer one.
        import time
        self.rule("ACCEPT", time.time())
        self.rule("REJECT", time.time() + 5, head="b" * 40)
        self.assertEqual(self.sweep(), [])
        self.approve.assert_not_called()
        self.assertEqual(gate.ruled_approvals([CHANGES, TUCK], self.loop, fg.HEAD), [])
        self.rule("ACCEPT", time.time() + 10)            # ruled again at the returned head
        self.assertEqual(gate.ruled_approvals([CHANGES, TUCK], self.loop, fg.HEAD), [TUCK])

    def test_an_accept_at_an_old_head_is_never_acted_on_at_the_current_one(self):
        # The PR moved on after an ACCEPT at "b"*40, whose change request is still listed.
        import time
        old = "b" * 40
        self.rule("ACCEPT", time.time())
        self.reviews = [tra.review(4, "CHANGES_REQUESTED", head=old), CHANGES]
        with mock.patch.object(ruled_approval.views, "ruling_row",
                               return_value={"verdict": "ACCEPT", "head": old, "created": 4e9,
                                             "comment": "none", "comment_id": None,
                                             "run_id": "q1"}):
            self.assertEqual(self.sweep(), [])
        self.approve.assert_not_called()


class Superseded(Base):
    """Review of #1605, round 4, F7: a ruling that lands while the sweep reads GitHub."""

    def reject_during_hold_read(self):
        def read(*_a):
            self.ruled("REJECT")              # a newer adjudication lands mid-sweep
            return (None, "")
        return mock.patch.object(pr_hold, "read", side_effect=read)

    def test_no_approval_on_an_accept_superseded_mid_sweep(self):
        self.ruled("ACCEPT")
        with self.reject_during_hold_read():
            self.assertEqual(self.sweep(), [])
        self.approve.assert_not_called()
        self.assertIsNone(self.row())

    def test_no_dismissal_on_an_accept_superseded_mid_sweep(self):
        self.ruled("ACCEPT")
        self.sweep()                          # approved
        with self.reject_during_hold_read():
            self.sweep()
        self.dismiss.assert_not_called()
        self.assertEqual(self.row()["dismiss"], "pending")


class NewVerdict(Base):
    """Review of #1605, round 6, F10: a reviewer verdict that lands while the sweep reads."""

    def verdict_during_hold_read(self):
        def read(*_a):
            self.reviews = [*self.reviews, tra.review(29, "CHANGES_REQUESTED",
                                                      at="2026-01-04T00:00:00Z")]
            return (None, "")
        return mock.patch.object(pr_hold, "read", side_effect=read)

    def test_no_approval_after_a_verdict_that_lands_mid_sweep(self):
        self.ruled("ACCEPT")
        with self.verdict_during_hold_read():
            self.assertEqual(self.sweep(), [])
        self.approve.assert_not_called()
        self.assertIsNone(self.row())

    def test_no_dismissal_after_a_verdict_that_lands_mid_sweep(self):
        self.ruled("ACCEPT")
        self.sweep()                          # approved
        with self.verdict_during_hold_read():
            self.sweep()
        self.dismiss.assert_not_called()
        self.assertEqual(self.row()["dismiss"], "pending")


class Interrupted(Base):
    """Review of #1605, round 2, F3: a step left ``posting`` by a stopped process."""

    def claim(self, step, age):
        import sqlite3
        import time
        from contextlib import closing
        sup = run_supervisor.Supervisor(self.db)
        if step == "dismiss":
            sup.ruled_approval_step(fg.REPO, 7, fg.HEAD, "approve", "posting", overruled=5)
            sup.ruled_approval_step(fg.REPO, 7, fg.HEAD, "approve", "posted", review_id=9)
            self.reviews = [CHANGES, TUCK]
        sup.ruled_approval_step(fg.REPO, 7, fg.HEAD, step, "posting", overruled=5)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE ruled_approvals SET updated=?", (time.time() - age,))

    def test_an_interrupted_dismissal_that_landed_settles_as_posted_never_resent(self):
        self.ruled("ACCEPT")
        self.claim("dismiss", ruled_approval.POSTING_STALE_S + 1)
        self.reviews = [DISMISSED, TUCK]                   # it landed before the stop
        self.assertEqual(self.sweep(), [7])
        self.dismiss.assert_not_called()
        self.assertEqual(self.row()["dismiss"], "posted")

    def test_an_interrupted_approval_that_never_landed_settles_as_denied(self):
        self.ruled("ACCEPT")
        self.claim("approve", ruled_approval.POSTING_STALE_S + 1)
        self.assertEqual(self.sweep(), [7])
        self.approve.assert_not_called()
        self.assertEqual(self.row()["approve"], "denied")
        self.assertIn("interrupted", self.row()["error"])

    def test_a_fresh_posting_step_is_left_alone_but_keeps_the_check_red(self):
        from diaktoros import ready
        self.ruled("ACCEPT")
        self.claim("dismiss", 5)
        self.reviews = [DISMISSED, TUCK]
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.row()["dismiss"], "posting")
        lines = ready.acting(self.loop, 7, fg.HEAD, list(self.reviews), TUCK)
        self.assertTrue(lines and lines[0][0] == ready.FAILURE, lines)
        self.assertIn("unconfirmed (being written, or interrupted)", lines[0][1])


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

    def test_a_lost_approval_is_never_replayed(self):
        self.ruled("ACCEPT")
        self.approve.side_effect = TimeoutError("read timed out")
        self.sweep()
        self.assertEqual(self.row()["approve"], "uncertain")
        self.sweep()                                       # GitHub: no approval landed
        self.sweep()
        self.approve.assert_called_once()
        self.dismiss.assert_not_called()
        self.assertEqual(self.row()["approve"], "denied")

    def test_a_lost_dismissal_is_never_replayed_and_settles_from_github(self):
        self.ruled("ACCEPT")
        self.dismiss.side_effect = TimeoutError("read timed out")
        self.sweep()
        self.sweep()
        self.assertEqual((self.row()["approve"], self.row()["dismiss"]), ("posted", "uncertain"))
        self.sweep()                                       # GitHub: not dismissed after all
        self.dismiss.assert_called_once()
        self.assertEqual(self.row()["dismiss"], "denied")

    def test_a_lost_dismissal_that_landed_settles_as_posted(self):
        self.ruled("ACCEPT")
        def landed(*_a, review_id, **_kw):
            self._dismissed(review_id=review_id)
            raise TimeoutError("read timed out")
        self.dismiss.side_effect = landed
        self.sweep()
        self.sweep()
        self.assertEqual(self.row()["dismiss"], "uncertain")
        self.sweep()
        self.dismiss.assert_called_once()
        self.assertEqual(self.row()["dismiss"], "posted")

    def test_a_lost_approval_that_landed_settles_as_posted(self):
        self.ruled("ACCEPT")
        def landed(*a, **kw):
            self._approved()
            raise TimeoutError("read timed out")
        self.approve.side_effect = landed
        self.sweep()
        self.assertEqual(self.row()["approve"], "uncertain")
        self.sweep()
        self.assertEqual((self.row()["approve"], self.row()["review_id"]), ("posted", TUCK["id"]))
        self.approve.assert_called_once()

    def test_a_change_request_at_another_head_is_never_dismissed(self):
        self.ruled("ACCEPT")
        older = tra.review(3, "CHANGES_REQUESTED", login="critic", head="e" * 40)
        self.loop = {**self.loop, "reviewers": ["reviewer", "critic"]}
        self.reviews = [older, CHANGES]
        self.sweep()
        self.sweep()
        self.assertEqual([c.kwargs["review_id"] for c in self.dismiss.call_args_list], [5])
        self.assertIn("critic's changes_requested 3 at eeeeeee still blocks", self.logged[-1])

    def test_a_maintainers_change_request_is_never_dismissed(self):
        # Review of #1591, F4: a person's request at the same head is theirs to lift.
        self.ruled("ACCEPT")
        human = tra.review(12, "CHANGES_REQUESTED", login="coe0718", at="2026-01-03T00:00:00Z")
        self.reviews = [CHANGES, human]
        self.sweep()
        self.sweep()
        self.assertEqual([c.kwargs["review_id"] for c in self.dismiss.call_args_list], [5])
        self.assertIn("coe0718's changes_requested 12", self.logged[-1])

    def test_only_the_review_the_ruling_overruled_is_dismissed(self):
        # Review of #1605, F2: two reviewer accounts' requests at the head; the ruling overruled
        # the latest effective verdict only, so only that one is dismissed.
        self.ruled("ACCEPT")
        self.loop = {**self.loop, "reviewers": ["reviewer", "critic"]}
        other = tra.review(15, "CHANGES_REQUESTED", login="critic", at="2026-01-01T06:00:00Z")
        self.reviews = [CHANGES, other]
        self.sweep()
        self.sweep()
        self.assertEqual([c.kwargs["review_id"] for c in self.dismiss.call_args_list], [15])
        self.assertEqual(self.row()["overruled"], 15)

    def test_a_newer_verdict_after_the_approval_is_never_dismissed(self):
        # Review of #1605, F2: the reviewer asks again after the approval; it wins, and stays.
        self.ruled("ACCEPT")
        self.sweep()
        newer = tra.review(17, "CHANGES_REQUESTED", at="2026-01-03T00:00:00Z")
        self.reviews = [*self.reviews, newer]
        self.sweep()
        self.dismiss.assert_not_called()
        self.assertEqual(self.row()["dismiss"], "denied")
        self.assertIn("changes_requested 17", self.row()["error"])

    def test_the_reviewers_own_later_approval_leaves_nothing_to_dismiss(self):
        self.ruled("ACCEPT")
        self.sweep()
        self.reviews = [*self.reviews, tra.review(18, "APPROVED", at="2026-01-03T00:00:00Z")]
        self.sweep()
        self.dismiss.assert_not_called()
        self.assertEqual((self.row()["dismiss"], self.row()["error"]), ("none", None))

    def test_a_change_request_from_after_the_ruling_was_not_overruled(self):
        self.ruled("ACCEPT")
        self.reviews = [tra.review(5, "CHANGES_REQUESTED", at="2099-01-01T00:00:00Z")]
        self.assertEqual(self.sweep(), [])
        self.approve.assert_not_called()

    def test_the_dismissal_rechecks_every_condition(self):
        # Review of #1605, F1: an approval posted on an earlier sweep never dismisses past a
        # red check, a hold or a reserved path.
        for name, setup, state in (
                ("red", lambda: setattr(self, "ci", ci.CIState(failed=["tests"])), "pending"),
                ("unread", lambda: setattr(self, "ci", None), "pending"),
                ("hold", lambda: setattr(self, "hold", ({"by": "coe0718"}, "")), "pending"),
                ("paths", lambda: setattr(self, "paths", ("", "it touches .github/x")), "denied"),
                ("human", lambda: self.st.human_hold_set(7, fg.HEAD, "touches"), "denied")):
            with self.subTest(name):
                self.setUp()
                self.ruled("ACCEPT")
                self.sweep()
                self.assertEqual(self.row()["approve"], "posted")
                setup()
                self.sweep()
                self.dismiss.assert_not_called()
                self.assertEqual(self.row()["dismiss"], state)


class Overlap(Base):
    def test_a_fresh_approval_still_being_written_makes_the_sweep_wait(self):
        # Review of #1605, round 3, F6: never take the dismissal step past a posting approval.
        self.ruled("ACCEPT")
        sup = run_supervisor.Supervisor(self.db)
        sup.ruled_approval_step(fg.REPO, 7, fg.HEAD, "approve", "posting", overruled=5)
        self.assertEqual(self.sweep(), [])
        self.dismiss.assert_not_called()
        self.assertEqual((self.row()["approve"], self.row()["dismiss"]), ("posting", "pending"))
        sup.ruled_approval_step(fg.REPO, 7, fg.HEAD, "approve", "posted", review_id=9)
        self.reviews = [CHANGES, TUCK]                     # the approval landed
        self.sweep()
        self.dismiss.assert_called_once()


def ready_mod():
    from diaktoros import ready
    return ready


class CheckShowsIt(Base):
    """Review of #1605, F3: ready and the merge check never go green past an unconfirmed or
    refused step, and say why."""

    def kinds(self):
        from diaktoros import ready
        pr = {**fg.LIVE, "user": {"login": "fixer"}}
        with mock.patch.object(ready.gate, "shadow_state", return_value=("", "", "")), \
                mock.patch.object(ready.gate_failures, "open_for", return_value=[]):
            return ready.assess_kinds({**self.loop, "required_checks": []}, self.st, pr,
                                      list(self.reviews), ci.CIState(passed=[]), tra.CLEAN)

    def test_a_landed_but_unconfirmed_dismissal_keeps_the_check_red(self):
        self.mark_escalated(by="dispute")
        self.ruled("ACCEPT")
        def landed(*_a, review_id, **_kw):
            self._dismissed(review_id=review_id)
            raise TimeoutError("read timed out")
        self.dismiss.side_effect = landed
        self.sweep()
        self.sweep()
        kinds = self.kinds()                     # GitHub shows it dismissed; the ledger can't say
        self.assertEqual(merge_check.decide(kinds)[0], merge_check.FAILURE)
        self.assertIn("unconfirmed", kinds[-1][1])
        self.assertIn("read timed out", kinds[-1][1])
        self.sweep()                             # read back: posted, and now green
        self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.SUCCESS)

    def test_a_refused_dismissal_names_its_reason(self):
        self.mark_escalated(by="dispute")
        self.ruled("ACCEPT")
        self.sweep()
        with mock.patch.object(broker, "authorize_adjudicator",
                               side_effect=broker.BrokerDenied("HTTP 403 forbidden")):
            self.sweep()
        kinds = self.kinds()
        self.assertEqual(merge_check.decide(kinds)[0], merge_check.FAILURE)
        self.assertTrue(any("was not made: HTTP 403 forbidden" in text for _, text in kinds), kinds)

    def test_the_reviewers_own_approval_clears_a_denied_dismissal(self):
        # Review of #1605, round 4, F8: once the reviewer approves, its request is lifted by
        # its own approval, and the denied dismissal no longer holds the head.
        self.ruled("ACCEPT")
        self.sweep()
        with mock.patch.object(broker, "authorize_adjudicator",
                               side_effect=broker.BrokerDenied("HTTP 403 forbidden")):
            self.sweep()
        self.assertEqual(self.row()["dismiss"], "denied")
        self.reviews = [*self.reviews, tra.review(19, "APPROVED", at="2026-01-03T00:00:00Z")]
        kinds = self.kinds()
        self.assertFalse(any("was not made" in text for _, text in kinds), kinds)
        self.assertEqual(merge_check.decide(kinds)[0], merge_check.SUCCESS)

    def test_after_the_reviewers_approval_another_accounts_request_still_blocks(self):
        # Review of #1605, round 5, F9: the reviewer's approval lifts only its own request; a
        # maintainer's, which GitHub still counts, keeps the check red, and explain says so too.
        from diaktoros import explain
        self.ruled("ACCEPT")
        self.sweep()
        with mock.patch.object(broker, "authorize_adjudicator",
                               side_effect=broker.BrokerDenied("HTTP 403 forbidden")):
            self.sweep()
        human = tra.review(12, "CHANGES_REQUESTED", login="coe0718", at="2026-01-02T12:00:00Z")
        mine = tra.review(19, "APPROVED", at="2026-01-03T00:00:00Z")
        self.reviews = [*self.reviews, human, mine]
        kinds = self.kinds()
        line = "coe0718's changes-requested review 12 still blocks GitHub's merge until it is dismissed"
        self.assertEqual(kinds, [(ready_mod().FAILURE, line)])
        self.assertEqual(merge_check.decide(kinds)[0], merge_check.FAILURE)
        facts = {"pr": fg.LIVE, "reviews": list(self.reviews), "armed": True, "read_at": 0,
                 "shadow_hold": ("", "")}
        report = explain.explain(self.loop, self.st, 7, facts)
        self.assertIn(line, report["blockers"])
        self.assertNotIn("nothing —", report["next"]["action"])

    def test_a_plain_approval_with_no_acting_on_record_is_unchanged(self):
        # Where the sweep never acted, the merge check reads the head as it always has.
        human = tra.review(12, "CHANGES_REQUESTED", login="coe0718", at="2026-01-02T12:00:00Z")
        self.reviews = [CHANGES, human, tra.review(19, "APPROVED", at="2026-01-03T00:00:00Z")]
        self.assertEqual(self.kinds(), [])

    def test_an_unreadable_record_is_never_green(self):
        self.mark_escalated(by="dispute")
        self.ruled("ACCEPT")
        self.reviews = [DISMISSED, TUCK]
        with mock.patch.object(run_supervisor.views, "ruled_approval",
                               return_value=(None, "OperationalError: disk I/O error")):
            kinds = self.kinds()
        self.assertEqual(merge_check.decide(kinds)[0], merge_check.FAILURE)
        self.assertIn("can't be read", kinds[-1][1])


class Steps(Base):
    def test_a_step_only_moves_forward(self):
        sup = run_supervisor.Supervisor(self.db)
        step = lambda *a, **kw: sup.ruled_approval_step(fg.REPO, 7, fg.HEAD, *a, **kw)  # noqa: E731
        self.assertFalse(step("approve", "posted"))           # not before posting
        self.assertTrue(step("approve", "posting"))
        self.assertFalse(step("approve", "posting"))          # a second sweep claims nothing
        self.assertTrue(step("approve", "posted", review_id=9))
        self.assertFalse(step("approve", "uncertain"))
        self.assertTrue(step("dismiss", "posting"))
        self.assertTrue(step("dismiss", "uncertain"))
        self.assertFalse(step("dismiss", "posting"))          # never sent again
        self.assertTrue(step("dismiss", "posted"))            # only once GitHub was read
        self.assertFalse(step("dismiss", "denied"))
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
