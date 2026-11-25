"""#515: maintainers' review bodies and inline comments reach the seats; the reviewer accounts
for each point. Review on #2724: F1 (prompt + stranger), F2 (quotes/no evidence), F3 (seat
account that is also a maintainer)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import gh, issue_facts  # noqa: E402
import test_issue_facts as tif  # noqa: E402

LOOP = tif.LOOP
FOOTER = "\n\n---\n<sub>🤖 Automated by [Diaktoros](https://github.com/coe0718/diaktoros) · x</sub>"


def review(rid, login, body, at="2026-01-02T00:00:00Z", state="COMMENTED"):
    return {"id": rid, "user": {"login": login}, "body": body, "submitted_at": at, "state": state}


def inline(cid, login, body, at="2026-01-03T00:00:00Z"):
    return {"id": cid, "user": {"login": login}, "body": body, "created_at": at,
            "path": "a.py", "line": 4}


REQS = "## Requirements\n- first: met (test_x)\n- second: met (test_y)\n\n"


class Points(unittest.TestCase):
    def setUp(self):
        self.fake = tif.Fake()
        for target, fn in (("api", self.fake.api), ("issue_comments_read", self.fake.comments)):
            patch = mock.patch.object(gh, target, side_effect=fn)
            patch.start()
            self.addCleanup(patch.stop)

    def serve(self, reviews=(), comments=(), loop=LOOP):
        tif.quiet_reviews(self, reviews, comments)
        return loop

    def test_review_bodies_and_inline_comments_reach_the_section_strangers_do_not(self):
        self.serve([review(3, "boss", "REVIEW-BODY"), review(4, "stranger", "STRANGER-REVIEW")],
                   [inline(5, "boss", "INLINE-NOTE"), inline(6, "stranger", "STRANGER-INLINE")])
        text = issue_facts.section(LOOP, 7)
        for want in ("REVIEW-BODY", "point `MR3`", "INLINE-NOTE", "point `MI5`", "a.py:4"):
            self.assertIn(want, text)
        self.assertNotIn("STRANGER", text)

    def test_an_approve_must_give_each_point_a_status_with_evidence(self):
        self.serve([review(3, "boss", "please add X")])
        self.assertIn("MR3", issue_facts.check(LOOP, 7, "APPROVE", REQS))
        ok = REQS + "## Maintainer points\n- MR3: addressed — test_x covers X\n"
        self.assertEqual(issue_facts.check(LOOP, 7, "APPROVE", ok), "")
        bare = REQS + "## Maintainer points\n- MR3: addressed\n"
        self.assertIn("needs evidence", issue_facts.check(LOOP, 7, "APPROVE", bare))

    def test_the_last_status_on_a_point_line_counts(self):
        # Review on #2724 F2 (round 2): `addressed? no — not addressed` is not addressed.
        self.serve([review(3, "boss", "please add X")])
        body = REQS + "## Maintainer points\n- MR3: addressed? no — not addressed\n"
        self.assertIn("not addressed", issue_facts.check(LOOP, 7, "APPROVE", body))

    def test_a_quoted_or_out_of_section_status_is_no_status(self):
        # F2: a quotation of an earlier status, or a mention in prose, accounts for nothing.
        self.serve([review(3, "boss", "please add X")])
        for body in (REQS + "> MR3: addressed — the fixer said so\n",
                     REQS + "The fixer wrote that MR3: addressed — done.\n",
                     REQS + "## Maintainer points\n> MR3: addressed — quoted\n",
                     REQS + "## Maintainer points\n```\nMR3: addressed — code\n```\n"):
            with self.subTest(body=body):
                self.assertIn("MR3", issue_facts.check(LOOP, 7, "APPROVE", body))

    def test_a_seat_account_that_is_a_maintainer_keeps_its_hand_written_points(self):
        # F3: only the loop's own (footered) output from that account is dropped.
        loop = {**LOOP, "reviewers": ["boss"]}
        self.serve([review(3, "boss", "HAND-REVIEW"), review(8, "boss", "SEAT-VERDICT" + FOOTER)],
                   [inline(5, "boss", "HAND-INLINE")], loop)
        points, _ = issue_facts.pr_points(loop, 7)
        ids = [point for *_, point in points]
        self.assertIn("MR3", ids)
        self.assertIn("MI5", ids)
        self.assertNotIn("MR8", ids)
        other = {**LOOP, "reviewers": ["review"]}
        tif.quiet_reviews(self, [review(9, "review", "PLAIN-SEAT")])
        self.assertNotIn("MR9", [p for *_, p in issue_facts.pr_points(other, 7)[0]])


if __name__ == "__main__":
    unittest.main()
