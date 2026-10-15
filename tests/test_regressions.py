"""Approved, then broke (#661): `hermes dk regression` links a later bug to the approved PR
that shipped it, only as far as GitHub confirms it, and `report` counts the links.

GitHub is faked (``gh.api`` for the issue, ``gh.pr``, ``gh.reviews``); the run ledger is real.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import cli, gh, ledger, run_supervisor, stats  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

HEAD, EARLIER, LATER = fg.HEAD, "e" * 40, "f" * 40
MERGED = {**fg.LIVE, "state": "closed", "merged_at": "2026-03-01T00:00:00Z"}
BUG = {"number": 90, "title": "widgets vanish", "created_at": "2026-03-05T00:00:00Z"}


def approval(rid=11, head=HEAD, at="2026-02-20T00:00:00Z", state="APPROVED"):
    return {**fg.verdict(rid, state), "commit_id": head, "submitted_at": at}


class Links(fg.Base):
    def setUp(self):
        super().setUp()
        self.issue, self.pr, self.reviews = dict(BUG), dict(MERGED), [approval()]
        self.db = run_supervisor.production_ledger()
        self.sup = Supervisor(self.db)

    def link(self, issue=90, pr=7, retract=False):
        def api(loop, path, *a, **k):
            return self.issue if path.endswith(f"/issues/{issue}") else None
        with mock.patch.object(gh, "api", side_effect=api), \
             mock.patch.object(gh, "pr", side_effect=lambda *a, **k: self.pr), \
             mock.patch.object(gh, "reviews", side_effect=lambda *a, **k: self.reviews), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_regression(argparse.Namespace(loop="one", issue=issue, pr=pr,
                                                       retract=retract))
        return rc, out.getvalue()

    def rows(self):
        with ledger.connect(self.db) as con:
            return con.execute("SELECT issue, pr, head, review_id, model, state "
                               "FROM regressions").fetchall()

    def reviewed(self, model, review_id):
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue("rv", fg.REPO, 7, HEAD, "reviewer")
        with ledger.connect(self.db) as con:
            run_id = con.execute("SELECT id FROM runs WHERE delivery='rv'").fetchone()[0]
            con.execute("UPDATE runs SET model=? WHERE id=?", (model, run_id))
            con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,"
                        "review_id,verdict,created) VALUES(?,?,?,?,?,?,?)",
                        (run_id, "confirmed", "{}", 1, review_id, "APPROVE", 0))



class Command(Links):
    def test_a_verified_link_names_the_approval_head_and_model(self):
        self.reviewed("model-a", 11)
        rc, out = self.link()
        self.assertEqual(rc, 0, out)
        self.assertIn("linked — approved at aaaaaaa (review 11, model model-a)", out)
        self.assertEqual(self.rows(), [(90, 7, HEAD, 11, "model-a", "linked")])
        rc, out = self.link()                                    # recorded twice: one link
        self.assertIn("already", out)
        self.assertEqual(len(self.rows()), 1)

    def test_a_retraction_is_explicit_and_kept(self):
        self.link()
        rc, out = self.link(retract=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.rows()[0][-1], "retracted")
        rc, out = self.link(retract=True)
        self.assertEqual(rc, 1)                                  # nothing live to retract
        rc, out = self.link()
        self.assertIn("relinked", out)
        self.assertEqual(self.rows()[0][-1], "linked")

    def test_what_github_does_not_confirm_is_refused(self):
        cases = [
            ("a bug older than the merge (pre-existing)",
             lambda: self.issue.update(created_at="2026-02-01T00:00:00Z"), "pre-existing defect"),
            ("an unmerged PR", lambda: self.pr.pop("merged_at"), "did not merge"),
            ("a pull request as the bug", lambda: self.issue.update(pull_request={}),
             "is a pull request"),
            ("approved at an earlier head than merged",
             lambda: self.reviews.__setitem__(0, approval(head=EARLIER)), "earlier head"),
            ("approved, then changes requested at the merged head",
             lambda: self.reviews.append(approval(12, at="2026-02-21T00:00:00Z",
                                                  state="CHANGES_REQUESTED")), "did not approve"),
            ("approved by someone who is not the loop's reviewer",
             lambda: self.reviews[0].update(user={"login": "stranger"}), "did not approve"),
        ]
        for name, change, reason in cases:
            with self.subTest(name):
                self.issue, self.pr, self.reviews = dict(BUG), dict(MERGED), [approval()]
                change()
                rc, out = self.link()
                self.assertEqual(rc, 1, out)
                self.assertIn(reason, out)
                self.assertEqual(self.rows(), [])

    def test_unreadable_github_records_nothing(self):
        self.issue = None
        rc, out = self.link()
        self.assertEqual((rc, self.rows()), (2, []))
        self.assertIn("could not be read", out)
        self.issue, self.reviews = dict(BUG), None
        rc, out = self.link()
        self.assertEqual((rc, self.rows()), (2, []))


class Report(Links):
    def test_report_counts_approved_prs_not_bug_reports(self):
        self.reviewed("model-a", 11)
        self.link(90, 7)
        self.issue = {**BUG, "number": 91}
        self.link(91, 7)                                         # a second report, same PR
        self.issue = {**BUG, "number": 92}
        self.link(92, 7)
        self.link(92, 7, retract=True)                           # withdrawn
        loop = {"repo": fg.REPO, "id": "one"}
        data = stats.report(loop, self.db, 0, 4e9, breaches={}, pr_state=lambda n: None,
                            tracked={})
        broke = data["regressions"]
        self.assertEqual((broke["prs"], broke["issues"], broke["retracted"]), (1, 2, 1))
        self.assertEqual(broke["models"], {"model-a": 1})
        out = stats.report_text(data)
        self.assertIn("approved, then broke: 1 PRs (2 bug reports) · model-a 1 · 1 retracted", out)
        self.assertIn("#90 ← PR #7 approved at aaaaaaa (model-a)", out)
        self.assertEqual(json.loads(json.dumps(data))["regressions"]["prs"], 1)
        earlier = stats.regressions(self.db, fg.REPO, 0, 1)      # a window before any link
        self.assertEqual((earlier["prs"], earlier["issues"]), (0, 0))
        self.assertIsNotNone(earlier["from"])                    # but linking had begun

    def test_no_link_yet_says_so_rather_than_zero(self):
        data = stats.report({"repo": fg.REPO, "id": "one"}, self.db, 0, 4e9, breaches={},
                            pr_state=lambda n: None, tracked={})
        self.assertIn("none linked yet (`hermes dk regression`)", stats.report_text(data))


if __name__ == "__main__":
    unittest.main()
