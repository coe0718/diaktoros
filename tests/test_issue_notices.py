#!/usr/bin/env python3
"""#231: the observer says what happened to an issue, and a failed run says so at once.

Live, an issue triaged to "nothing" and an issue fix that failed four times both left the feed
silent; the only word was the watchdog's operator notice after the last retry, linking /pull/
for an issue. Now:

- ``triaged``: the labels applied, or that none fit, or why nothing was written;
- ``fixing``: an issue handed to the fixer (or held, and why);
- ``fixed``: the PR opened and review requested, or the fixer's could-not-fix comment, or an
  uncertain write to inspect;
- ``failed``: any isolated run's first failed attempt (it will retry) and its terminal state.

Issue notices link /issues/N and carry no head. Facts only: host-written outcomes, never model
text.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import pathlib
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_issue_fix as fixture  # noqa: E402
from review_loop import broker_ipc, config, ledger, observer, run_supervisor  # noqa: E402
from review_loop.state import LoopState  # noqa: E402

REPO, BASE = fixture.REPO, fixture.BASE


class Rendering(unittest.TestCase):
    loop = {"id": "widgets", "repo": REPO, "cap": 3}

    def test_issue_events_link_the_issue_and_carry_no_head(self):
        for event in ("triaged", "fixing", "fixed"):
            with self.subTest(event=event):
                self.assertIn(event, observer.EVENTS)
                text = observer.summarize(self.loop, event, 271, BASE, outcome="x", issue=True)
                self.assertNotIn(BASE[:7], text)
                self.assertIn("#271", text)
        self.assertEqual(observer.link(self.loop, 271, issue=True),
                         f"https://github.com/{REPO}/issues/271")
        self.assertEqual(observer.link(self.loop, 7), f"https://github.com/{REPO}/pull/7")
        self.assertIn("failed", observer.EVENTS)


class Outcomes(fixture.Base):
    def scope(self, run_id):
        return broker_ipc.RunScope(REPO, 12, BASE, "issue_fixer", "review-loop/issue-12",
                                   run_id, str(self.db))

    def test_issue_fix_outcomes_read_from_the_ledger(self):
        sup, run_id = self.fix_row()
        self.assertEqual(broker_ipc._issue_fix_outcome(self.scope(run_id)), "")  # no write yet
        sup.record_issue_fix(run_id, REPO, 12, BASE, "pr", "review-loop/issue-12")
        sup.issue_fix_status(run_id, "opened", pr_number=280)
        self.assertEqual(broker_ipc._issue_fix_outcome(self.scope(run_id)), "PR #280 opened")
        sup.issue_fix_status(run_id, "requested")
        self.assertEqual(broker_ipc._issue_fix_outcome(self.scope(run_id)),
                         "PR #280 opened, review requested")

    def test_an_uncertain_push_says_to_inspect(self):
        sup, run_id = self.fix_row()
        sup.record_issue_fix(run_id, REPO, 12, BASE, "pr", "review-loop/issue-12")
        sup.issue_fix_status(run_id, "uncertain", error="push unknown")
        outcome = broker_ipc._issue_fix_outcome(self.scope(run_id))
        self.assertIn("uncertain: push unknown", outcome)
        self.assertIn("never replayed", outcome)

    def test_a_could_not_fix_comment(self):
        sup, run_id = self.fix_row()
        sup.record_issue_fix(run_id, REPO, 12, BASE, "comment", "review-loop/issue-12")
        sup.issue_fix_status(run_id, "posted", comment_id=5)
        self.assertIn("could not fix", broker_ipc._issue_fix_outcome(self.scope(run_id)))

    def triage_row(self, number):
        sup = run_supervisor.Supervisor(self.db)
        sup.enqueue(f"{REPO}:{number}:issue:triage:triage", REPO, number, config.TRIAGE_HEAD,
                    "triage", turn_key="triage")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='running', launch_intent=? WHERE seat='triage' "
                        "AND pr=?", (time.time(), number))
            return sup, con.execute("SELECT id FROM runs WHERE seat='triage' AND pr=?",
                                    (number,)).fetchone()[0]

    def test_triage_outcomes(self):
        sup, run_id = self.triage_row(12)
        sup.record_triage(run_id, REPO, 12, ["bug", "P3"], "")
        sup.triage_status(run_id, "posted")
        self.assertEqual(broker_ipc._triage_outcome(sup, run_id), "labelled bug, P3")
        sup, run2 = self.triage_row(13)
        sup.record_triage(run2, REPO, 13, [], "")
        sup.triage_status(run2, "nothing")
        self.assertIn("no allowed label fit", broker_ipc._triage_outcome(sup, run2))


    def test_a_triage_that_writes_nothing_is_still_announced(self):
        """Live #270: a `nothing` triage looked exactly like one that never ran."""
        sup, run_id = self.triage_row(14)
        sup.record_triage(run_id, REPO, 14, [], "")
        scope = broker_ipc.RunScope(REPO, 14, config.TRIAGE_HEAD, "triage", "", run_id,
                                    str(self.db))
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
             mock.patch.object(observer, "notify", return_value=True) as notify:
            broker_ipc._deliver_triage(self.loop, scope, sup, [], "")
        [call] = notify.call_args_list
        self.assertEqual(call[0][2], "triaged")
        self.assertEqual(call[1]["identity"], run_id)
        self.assertTrue(call[1]["issue"])
        self.assertIn("no allowed label fit", call[1]["outcome"])


class FailureNotice(fixture.Base):
    def notices(self, sup, run_id, state):
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
             mock.patch.object(observer, "notify", return_value=True) as notify:
            sup.failure_notice(run_id, state)
        return notify.call_args_list

    def set_run(self, state, retries, error="turn exited with status 1"):
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state=?, retries=?, error=?, retry_at=? "
                        "WHERE seat='issue_fixer'", (state, retries, error, time.time() + 120))

    def test_first_failure_and_terminal_states_notify_once_each_kind(self):
        sup, run_id = self.fix_row()
        self.set_run("waiting", 1)
        [call] = self.notices(sup, run_id, "waiting")
        args, kwargs = call
        self.assertEqual(args[2], "failed")
        self.assertEqual(kwargs["identity"], f"{run_id}:first")
        self.assertTrue(kwargs["issue"])
        self.assertIn("attempt 1 failed: turn exited with status 1 — retrying at", kwargs["outcome"])
        self.set_run("waiting", 2)
        self.assertEqual(self.notices(sup, run_id, "waiting"), [])        # later retries: quiet
        self.set_run("waiting", 0)
        self.assertEqual(self.notices(sup, run_id, "waiting"), [])        # waiting, no error kind
        self.set_run("failed", 4, "retry limit (4 attempts): turn exited with status 1")
        [call] = self.notices(sup, run_id, "failed")
        self.assertEqual(call[1]["identity"], f"{run_id}:failed")
        self.assertIn("issue_fixer failed: retry limit", call[1]["outcome"])
        self.assertEqual(self.notices(sup, run_id, "succeeded"), [])

    def test_a_hold_is_announced_once_with_when_and_how_to_run_it_sooner(self):
        """Live 2026-10-04: two issue fixes sat behind the daily cap with only a 'queued'
        notice; it looked like the fixer was working. A hold is not a failure, so it gets its
        own event."""
        sup, run_id = self.fix_row()
        held = "held: issue_fixer daily turn cap (10) reached — resumes 2026-10-05 00:00"
        self.set_run("waiting", 0, held)
        [call] = self.notices(sup, run_id, "waiting")
        args, kwargs = call
        self.assertEqual(args[2], "held")
        self.assertTrue(kwargs["issue"])
        self.assertTrue(kwargs["identity"].startswith(f"{run_id}:held:"))
        self.assertIn("daily turn cap (10) reached — resumes 2026-10-05 00:00", kwargs["outcome"])
        self.assertIn("`triage --fix-daily-turns N`", kwargs["outcome"])
        self.assertIn("`retry --pr 12 --seat issue_fixer`", kwargs["outcome"])
        # A usage-window hold: when it resumes, no cap advice (raising a cap would not help).
        window = "held: fixer usage window (anthropic) — resumes 18:40"
        self.set_run("waiting", 2, window)              # a hold even after spent retries
        [call] = self.notices(sup, run_id, "waiting")
        self.assertEqual(call[0][2], "held")
        self.assertIn("usage window", call[1]["outcome"])
        self.assertNotIn("raise the cap", call[1]["outcome"])
        self.assertIn("held", observer.EVENTS)
        self.assertEqual(observer.EMOJI["held"], "⏸")

    def test_the_operator_notice_links_an_issue_as_an_issue(self):
        sup, run_id = self.fix_row()
        row = {"repo": REPO, "pr": 12, "head": BASE, "seat": "issue_fixer", "id": run_id}
        current = {"state": "failed", "error": "x", "detail": "", "retries": 4}
        text = run_supervisor.Supervisor._notice_message(row, current, None)
        self.assertIn(f"https://github.com/{REPO}/issues/12", text)
        row["seat"] = "reviewer"
        self.assertIn(f"https://github.com/{REPO}/pull/12",
                      run_supervisor.Supervisor._notice_message(row, current, None))


if __name__ == "__main__":
    unittest.main()
