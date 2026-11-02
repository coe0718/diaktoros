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


def review(rid, state, login="reviewer", at="2026-01-01T00:00:00Z", head=fg.HEAD, pr=7):
    return {"id": rid, "state": state, "commit_id": head, "submitted_at": at,
            "user": {"login": login, "id": rid},
            "pull_request_url": f"https://api.github.com/repos/{fg.REPO}/pulls/{pr}"}


CHANGES = review(5, "CHANGES_REQUESTED")
DISMISSED = review(5, "DISMISSED")
TUCK = review(9, "APPROVED", login="adj", at="2026-01-02T00:00:00Z")


class Base(ser.Base):
    def setUp(self):
        super().setUp()
        self.loop = {**self.loop, "seats": {**self.loop["seats"], "adjudicator": {"login": "adj"}}}

    def ruled_on(self, pr, verdict):
        """A ruling on another PR at the same head (``ser.Base.ruled`` writes PR 7's)."""
        import sqlite3
        import time
        from contextlib import closing
        self.n += 1
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO rulings(run_id,repo,pr,head,turn_key,verdict,body,created,"
                        "updated) VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"o{self.n}", fg.REPO, pr, fg.HEAD, "breach:1", verdict, "because",
                         time.time() + self.n, time.time() + self.n))

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

    def test_an_undatable_or_unfamiliar_adjudicator_verdict_counts_nothing(self):
        # Review of #1591, F3: unknown order is never "earlier"; nothing of that login counts.
        self.ruled("ACCEPT")
        for bad in (review(10, "CHANGES_REQUESTED", login="adj", at=None),
                    review(10, "CHANGES_REQUESTED", login="adj", at="2026-01-03T00:00:00"),
                    review(10, "APPROVED", login="adj", at="soon"),
                    review(0, "CHANGES_REQUESTED", login="adj"),
                    review(10, "WEIRD", login="adj", at="2026-01-01T12:00:00Z"),  # older
                    {**TUCK, "id": 9}):                               # a tie with TUCK
            with self.subTest(bad=bad):
                self.assertEqual(gate.ruled_approvals([DISMISSED, TUCK, bad], self.loop, fg.HEAD),
                                 [])
                self.assertIsNone(self.latest([DISMISSED, TUCK, bad]))

    def test_a_maintainers_change_request_blocks_like_the_reviewers(self):
        # Review of #1591, F4: GitHub counts any write-access login's request, not only seats'.
        human = review(12, "CHANGES_REQUESTED", login="coe0718", at="2026-01-03T00:00:00Z")
        self.assertEqual(gate.blocking_reviews([DISMISSED, TUCK, human]), [human])
        lifted = review(13, "APPROVED", login="coe0718", at="2026-01-04T00:00:00Z")
        self.assertEqual(gate.blocking_reviews([DISMISSED, TUCK, human, lifted]), [])

    def test_a_change_request_with_no_login_still_blocks(self):
        # #1591's shadow review: a review whose user is missing is never silently dropped.
        ghost = {**review(14, "CHANGES_REQUESTED", at="2026-01-03T00:00:00Z"), "user": None}
        self.assertEqual(gate.blocking_reviews([DISMISSED, TUCK, ghost]), [ghost])
        nameless = {**ghost, "id": 15, "user": {"login": ""}}
        self.assertEqual(gate.blocking_reviews([DISMISSED, TUCK, nameless]), [nameless])

    def test_an_undatable_change_request_still_blocks(self):
        undated = review(10, "CHANGES_REQUESTED", login="adj", at=None)
        self.assertEqual(gate.blocking_reviews([DISMISSED, TUCK, undated]), [undated])
        old = review(4, "CHANGES_REQUESTED", at=None)
        self.assertEqual(gate.blocking_reviews([old, review(6, "APPROVED")]), [old])

    def test_an_unreadable_ledger_counts_nothing(self):
        self.ruled("ACCEPT")
        with mock.patch.object(views, "read_only_connect", return_value=None):
            self.assertIs(self.latest([CHANGES, TUCK]), CHANGES)

    def test_a_ruling_on_another_pr_at_the_same_commit_backs_nothing(self):
        # Review of #1591, F1: a ruling is the PR's; the same commit on PR 8 decides nothing here.
        self.ruled_on(8, "ACCEPT")
        self.assertIs(self.latest([CHANGES, TUCK]), CHANGES)
        self.ruled("ACCEPT")
        self.ruled_on(8, "REJECT")                     # nor does PR 8's later REJECT cancel it
        self.assertIs(self.latest([CHANGES, TUCK]), TUCK)

    def test_an_approval_that_does_not_name_its_pr_counts_for_nothing(self):
        self.ruled("ACCEPT")
        for url in (None, "", "https://api.github.com/repos/other/repo/pulls/7",
                    f"https://api.github.com/repos/{fg.REPO}/pulls/0"):
            with self.subTest(url=url):
                self.assertIs(self.latest([CHANGES, {**TUCK, "pull_request_url": url}]), CHANGES)

    def test_the_adjudicators_later_change_request_withdraws_its_approval(self):
        # Review of #1591, F2: only the adjudicator's newest verdict at the head counts.
        self.ruled("ACCEPT")
        later = review(10, "CHANGES_REQUESTED", login="adj", at="2026-01-03T00:00:00Z")
        self.assertIsNone(self.latest([DISMISSED, TUCK, later]))
        self.assertEqual(gate.blocking_reviews([DISMISSED, TUCK, later]), [later])
        self.assertFalse(gate.reviewed_at_head([DISMISSED, TUCK, later], self.loop, fg.HEAD))
        comment = review(11, "COMMENTED", login="adj", at="2026-01-04T00:00:00Z")
        self.assertIs(self.latest([DISMISSED, TUCK, comment]), TUCK)


class Blocking(Base):
    def test_githubs_count_is_each_reviewers_newest_review(self):
        self.assertEqual(gate.blocking_reviews([CHANGES, TUCK]), [CHANGES])
        self.assertEqual(gate.blocking_reviews([DISMISSED, TUCK]), [])
        later = review(12, "APPROVED", at="2026-01-04T00:00:00Z", head="d" * 40)
        self.assertEqual(gate.blocking_reviews([CHANGES, later]), [])
        comment = review(13, "COMMENTED", at="2026-01-05T00:00:00Z")
        self.assertEqual(gate.blocking_reviews([CHANGES, comment]), [CHANGES])


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

    def test_no_green_for_another_prs_ruling_or_a_withdrawn_approval(self):
        # Review of #1591, F1 and F2, end to end through ready and the merge check.
        self.mark_escalated(by="dispute")
        self.ruled_on(8, "ACCEPT")
        self.assertEqual(merge_check.decide(self.kinds([DISMISSED, TUCK]))[0],
                         merge_check.PENDING)              # no verdict yet: never green
        self.ruled("ACCEPT")
        self.assertEqual(merge_check.decide(self.kinds([DISMISSED, TUCK]))[0],
                         merge_check.SUCCESS)
        later = review(10, "CHANGES_REQUESTED", login="adj", at="2026-01-03T00:00:00Z")
        self.assertEqual(merge_check.decide(self.kinds([DISMISSED, TUCK, later]))[0],
                         merge_check.FAILURE)

    def test_no_green_for_an_undatable_change_request(self):
        # Review of #1591, F3, end to end: the merge check is never green on an unproved order.
        self.mark_escalated(by="dispute")
        self.ruled("ACCEPT")
        for undated in (review(10, "CHANGES_REQUESTED", login="adj", at=None),
                        review(10, "CHANGES_REQUESTED", at=None)):
            with self.subTest(undated=undated):
                self.assertNotEqual(merge_check.decide(self.kinds([DISMISSED, TUCK, undated]))[0],
                                    merge_check.SUCCESS)

    def test_no_green_while_a_maintainer_still_requests_changes(self):
        # Review of #1591, F4, end to end.
        self.mark_escalated(by="dispute")
        self.ruled("ACCEPT")
        human = review(12, "CHANGES_REQUESTED", login="coe0718", at="2026-01-03T00:00:00Z")
        kinds = self.kinds([DISMISSED, TUCK, human])
        self.assertEqual(merge_check.decide(kinds)[0], merge_check.FAILURE)
        self.assertIn("coe0718's changes-requested review 12", kinds[0][1])

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

    def test_an_undismissed_change_request_is_named_never_merge_ready(self):
        # #1591's shadow review: explain agrees with ready and the merge check.
        self.mark_escalated(by="dispute")
        self.ruled("ACCEPT")
        facts = {"pr": fg.LIVE, "reviews": [CHANGES, TUCK], "armed": True, "read_at": 0,
                 "shadow_hold": ("", "")}
        with mock.patch.object(explain, "_ruled", return_value="ACCEPT"):
            report = explain.explain(self.loop, self.st, 7, facts)
        line = ("approved on the adjudicator's ACCEPT ruling, but reviewer's changes-requested "
                "review 5 still blocks GitHub's merge until it is dismissed")
        self.assertIn(line, report["blockers"])
        self.assertTrue(report["next"]["action"].startswith(line), report["next"])
        self.assertNotIn("nothing —", report["next"]["action"])


if __name__ == "__main__":
    unittest.main()
