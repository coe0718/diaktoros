"""#905: the adjudicator's approval on its ACCEPT ruling counts as the head's verdict.

An ACCEPT overrules the reviewer's change request. The adjudicator then approves the head as its
own login, and the reviewer's blocking review is dismissed. The loop's readers count that
approval only while the newest ruling at the head is ACCEPT (``gate.ruled_approvals``): a bare
approval by that login counts for nothing. The dismissed head still reads as reviewed
(``reviewed_at_head``), so no fresh review starts. ``ready`` and the merge check then stop waiting
for a person, but stay red while GitHub still counts the reviewer's change request
(``gate.blocking_reviews``), and ``explain`` stops reporting the head as parked.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
import test_shadow_escalate_ruling as ser  # noqa: E402
from diaktoros import ci, explain, gate, merge_check, ready  # noqa: E402
from diaktoros.run_supervisor import views  # noqa: E402

GREEN = ci.CIState(passed=[])
CLEAN = {"mergeable": True, "mergeable_state": "clean", "number": 7, "state": "open",
         "draft": False, "head": {"sha": fg.HEAD}, "base": {"ref": "main"}}


def review(rid, state, login="reviewer", at="2026-01-01T00:00:00Z", head=fg.HEAD):
    return {"id": rid, "state": state, "commit_id": head, "submitted_at": at,
            "user": {"login": login, "id": rid}}


CHANGES = review(5, "CHANGES_REQUESTED")
DISMISSED = review(5, "DISMISSED")
TUCK = review(9, "APPROVED", login="adj", at="2026-01-02T00:00:00Z")


class Base(ser.Base):
    def setUp(self):
        super().setUp()
        self.loop = {**self.loop, "seats": {**self.loop["seats"], "adjudicator": {"login": "adj"}}}

    def latest(self, reviews):
        return gate.latest_effective_review_at_head(reviews, self.loop, fg.HEAD)


class Counting(Base):
    def test_an_approval_on_an_accept_is_the_heads_verdict(self):
        self.ruled("ACCEPT")
        self.assertIs(self.latest([CHANGES, TUCK]), TUCK)
        self.assertIs(self.latest([DISMISSED, TUCK]), TUCK)
        self.assertTrue(gate.is_ruled_approval(TUCK, self.loop))
        self.assertFalse(gate.is_ruled_approval(CHANGES, self.loop))

    def test_a_bare_approval_by_the_adjudicator_counts_for_nothing(self):
        self.assertIs(self.latest([CHANGES, TUCK]), CHANGES)
        self.assertIsNone(self.latest([DISMISSED, TUCK]))

    def test_a_later_reject_voids_it(self):
        self.ruled("ACCEPT")
        self.ruled("REJECT")
        self.assertIs(self.latest([CHANGES, TUCK]), CHANGES)

    def test_only_an_accept_at_this_head_backs_an_approval_at_this_head(self):
        self.ruled("ACCEPT", head="b" * 40)
        self.assertIs(self.latest([CHANGES, TUCK]), CHANGES)
        self.ruled("ACCEPT")
        moved = review(9, "APPROVED", login="adj", at="2026-01-02T00:00:00Z", head="c" * 40)
        self.assertIs(self.latest([CHANGES, moved]), CHANGES)

    def test_no_adjudicator_login_or_a_reviewer_login_counts_nothing_extra(self):
        self.ruled("ACCEPT")
        bare = {**self.loop, "seats": {k: v for k, v in self.loop["seats"].items()
                                       if k != "adjudicator"}}
        self.assertIs(gate.latest_effective_review_at_head([CHANGES, TUCK], bare, fg.HEAD), CHANGES)
        self.assertEqual(gate.ruled_approvals([CHANGES, TUCK], {**self.loop, "reviewers":
                                                                ["reviewer", "adj"]}, fg.HEAD), [])

    def test_the_reviewers_newer_verdict_still_wins(self):
        self.ruled("ACCEPT")
        again = review(11, "CHANGES_REQUESTED", at="2026-01-03T00:00:00Z")
        self.assertIs(self.latest([CHANGES, TUCK, again]), again)

    def test_the_dismissed_head_still_reads_as_reviewed(self):
        # Without this, the reviewer gate would start a fresh review once the change request the
        # ACCEPT overruled is dismissed (gate_reviewer: "already has a reviewer's verdict").
        self.assertFalse(gate.reviewed_at_head([DISMISSED, TUCK], self.loop, fg.HEAD))
        self.ruled("ACCEPT")
        self.assertTrue(gate.reviewed_at_head([DISMISSED, TUCK], self.loop, fg.HEAD))
        self.assertFalse(gate.reviewed_at_head([DISMISSED], self.loop, fg.HEAD))

    def test_an_unreadable_ledger_counts_nothing(self):
        self.ruled("ACCEPT")
        with mock.patch.object(views, "read_only_connect", return_value=None):
            self.assertIs(self.latest([CHANGES, TUCK]), CHANGES)

    def test_ruling_at_head_reads_any_pr_at_the_head(self):
        self.assertIsNone(views.ruling_at_head(self.db, fg.REPO, fg.HEAD))
        self.ruled("REJECT")
        self.ruled("ACCEPT")
        self.assertEqual(views.ruling_at_head(self.db, fg.REPO, fg.HEAD), "ACCEPT")
        self.assertIsNone(views.ruling_at_head(self.db, "other/repo", fg.HEAD))


class Blocking(Base):
    def test_githubs_count_is_each_reviewers_newest_review(self):
        self.assertEqual(gate.blocking_reviews([CHANGES, TUCK], self.loop), [CHANGES])
        self.assertEqual(gate.blocking_reviews([DISMISSED, TUCK], self.loop), [])
        later = review(12, "APPROVED", at="2026-01-04T00:00:00Z", head="d" * 40)
        self.assertEqual(gate.blocking_reviews([CHANGES, later], self.loop), [])
        comment = review(13, "COMMENTED", at="2026-01-05T00:00:00Z")
        self.assertEqual(gate.blocking_reviews([CHANGES, comment], self.loop), [CHANGES])


class Ready(Base):
    def kinds(self, reviews):
        pr = {**fg.LIVE, "user": {"login": "fixer"}}
        with mock.patch.object(gate, "shadow_state", return_value=("", "", "")), \
                mock.patch.object(ready.gate_failures, "open_for", return_value=[]):
            return ready.assess_kinds({**self.loop, "required_checks": []}, self.st, pr, reviews,
                                      GREEN, CLEAN)

    def test_dismissed_and_approved_on_the_accept_is_green(self):
        self.mark_escalated(by="dispute")
        self.ruled("ACCEPT")
        kinds = self.kinds([DISMISSED, TUCK])
        self.assertEqual(kinds, [])
        self.assertEqual(merge_check.decide(kinds)[0], merge_check.SUCCESS)

    def test_an_undismissed_change_request_keeps_it_red_and_says_why(self):
        self.mark_escalated(by="dispute")
        self.ruled("ACCEPT")
        kinds = self.kinds([CHANGES, TUCK])
        self.assertEqual(kinds, [(ready.FAILURE, "approved on the adjudicator's ACCEPT ruling, "
                                  "but reviewer's changes-requested review 5 still blocks "
                                  "GitHub's merge until it is dismissed")])
        self.assertEqual(merge_check.decide(kinds)[0], merge_check.FAILURE)

    def test_an_accept_not_acted_on_still_waits_for_a_person(self):
        self.mark_escalated(by="dispute")
        self.ruled("ACCEPT")
        texts = [text for _, text in self.kinds([CHANGES])]
        self.assertTrue(any("changes requested at head" in t for t in texts), texts)
        self.assertTrue(any("ruled ACCEPT" in t and "a person decides" in t for t in texts), texts)


class Explain(Base):
    def test_an_acted_on_accept_is_not_reported_as_parked(self):
        self.mark_escalated(by="dispute")
        self.ruled("ACCEPT")
        facts = {"pr": fg.LIVE, "reviews": [DISMISSED, TUCK], "armed": True, "read_at": 0,
                 "shadow_hold": ("", "")}
        with mock.patch.object(explain, "_ruled", return_value="ACCEPT"):
            report = explain.explain(self.loop, self.st, 7, facts)
        self.assertFalse(any(b.startswith(("ruled:", "escalated:")) for b in report["blockers"]),
                         report["blockers"])
        self.assertIn("a human merges it", report["next"]["action"])


if __name__ == "__main__":
    unittest.main()
