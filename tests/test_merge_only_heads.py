"""A merge-only head keeps the change request it owes an answer to (#2743).

On 2026-11-25 a base merge (GitHub's Update branch) moved #2738 and #2745 from the head the
reviewer had requested changes at to a merge of main: the fixer's queued answer was cancelled as
superseded, the new head had no verdict, and nothing moved either PR. On #2700 the same kind of
head drew a sweep review that only repeated an unanswered finding, spending a verdict.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import gate, merge_only  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402
from diaktoros.state import ledger  # noqa: E402
import _ledger_guard  # noqa: E402,F401  refuses the operator's real ledger (#108)

A, B, C, BASE = "a" * 40, "b" * 40, "c" * 40, "e" * 40
LOOP = {"repo": "o/r", "base": "main", "read_token": "read", "reviewers": ["review"]}
PR_FILES = [{"filename": "diaktoros/x.py", "status": "modified", "sha": "1" * 40}]


def review(rid, state, head, minute=0):
    return {"id": rid, "state": state, "commit_id": head, "user": {"login": "review"},
            "submitted_at": f"2026-01-01T00:{minute:02d}:00Z"}


def sources(mapping):
    """A fake ``merge_only.source``: each head's merge-only parent, from ``mapping``."""
    return lambda loop, head: mapping.get(head)


def github(commits, compares):
    """A fake ``gh.api`` answering commit and compare reads from two maps (None: unreadable)."""
    def api(loop, path, *a, **k):
        if "/commits/" in path:
            return commits.get(path.rsplit("/", 1)[1])
        if "/compare/" in path:
            return compares.get(path.rsplit("/", 1)[1])
        raise AssertionError(f"unexpected read {path}")
    return api


class Source(unittest.TestCase):
    def setUp(self):
        merge_only._MEMO.clear()
        self.addCleanup(merge_only._MEMO.clear)

    def merge(self, before=PR_FILES, after=PR_FILES):
        commits = {B: {"parents": [{"sha": A}, {"sha": BASE}]}}
        compares = {f"{BASE}...{A}": {"files": before}, f"{BASE}...{B}": {"files": after}}
        return github(commits, compares)

    def test_a_merge_that_leaves_the_prs_diff_unchanged_is_merge_only(self):
        with patch("diaktoros.gh.api", side_effect=self.merge()):
            self.assertEqual(merge_only.source(LOOP, B), A)

    def test_a_merge_that_changed_a_pr_file_is_not(self):
        changed = [{**PR_FILES[0], "sha": "2" * 40}]
        with patch("diaktoros.gh.api", side_effect=self.merge(after=changed)):
            self.assertIsNone(merge_only.source(LOOP, B))

    def test_a_commit_that_is_not_a_merge_is_not(self):
        api = github({B: {"parents": [{"sha": A}]}}, {})
        with patch("diaktoros.gh.api", side_effect=api):
            self.assertIsNone(merge_only.source(LOOP, B))

    def test_a_diff_too_large_to_list_is_not(self):
        many = [{"filename": f"f{i}.py", "status": "modified", "sha": "1" * 40}
                for i in range(merge_only.FILES_MAX)]
        with patch("diaktoros.gh.api", side_effect=self.merge(before=many, after=many)):
            self.assertIsNone(merge_only.source(LOOP, B))

    def test_an_unreadable_compare_is_not_and_is_read_again(self):
        commits = {B: {"parents": [{"sha": A}, {"sha": BASE}]}}
        with patch("diaktoros.gh.api", side_effect=github(commits, {f"{BASE}...{A}": None})):
            self.assertIsNone(merge_only.source(LOOP, B))
        with patch("diaktoros.gh.api", side_effect=self.merge()):
            self.assertEqual(merge_only.source(LOOP, B), A)

    def test_a_chain_of_merges_leads_back_to_the_reviewed_head(self):
        with patch.object(merge_only, "source", side_effect=sources({C: B, B: A})):
            self.assertTrue(merge_only.leads_to(LOOP, C, A))
            self.assertFalse(merge_only.leads_to(LOOP, C, "d" * 40))


class Verdicts(unittest.TestCase):
    """``gate`` reads the carried change request wherever it reads a head's verdicts."""

    def test_the_change_request_stands_at_the_merge_only_head(self):
        reviews = [review(1, "CHANGES_REQUESTED", A)]
        with patch.object(merge_only, "source", side_effect=sources({B: A})):
            self.assertTrue(gate.reviewed_at_head(reviews, LOOP, B))
            latest = gate.latest_effective_review_at_head(reviews, LOOP, B)
        self.assertEqual((latest["state"], latest["carried_from"]), ("CHANGES_REQUESTED", A))

    def test_the_sweep_and_explain_see_it_as_the_fixers_to_answer(self):
        # #2700: with no change request "at the head", the sweep's lost-request path delivered
        # a review that repeated the unanswered finding. changes_at_head is what both read.
        reviews = [review(1, "CHANGES_REQUESTED", A)]
        with patch.object(merge_only, "source", side_effect=sources({B: A})):
            self.assertEqual([r["carried_from"] for r in gate.changes_at_head(reviews, LOOP, B)], [A])
        with patch.object(merge_only, "source", return_value=None):
            self.assertEqual(gate.changes_at_head(reviews, LOOP, B), [])

    def test_it_follows_a_chain_of_merges(self):
        reviews = [review(1, "CHANGES_REQUESTED", A)]
        with patch.object(merge_only, "source", side_effect=sources({C: B, B: A})):
            self.assertTrue(gate.reviewed_at_head(reviews, LOOP, C))

    def test_an_approval_is_not_carried_here(self):
        reviews = [review(1, "CHANGES_REQUESTED", A), review(2, "APPROVED", A, minute=1)]
        with patch.object(merge_only, "source", side_effect=sources({B: A})):
            self.assertFalse(gate.reviewed_at_head(reviews, LOOP, B))

    def test_a_head_that_is_not_merge_only_has_no_verdict(self):
        reviews = [review(1, "CHANGES_REQUESTED", A)]
        with patch.object(merge_only, "source", return_value=None):
            self.assertFalse(gate.reviewed_at_head(reviews, LOOP, B))

    def test_no_read_when_the_head_has_its_own_verdict_or_nothing_could_be_carried(self):
        with patch.object(merge_only, "source") as source:
            gate.reviewed_at_head([review(1, "CHANGES_REQUESTED", B)], LOOP, B)
            gate.reviewed_at_head([review(1, "APPROVED", A)], LOOP, B)
        source.assert_not_called()


class FixerClaim(unittest.TestCase):
    """The fixer's answer turn, queued at the reviewed head, runs at the merge-only head."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": tmp.name,
                                      "DIAKTOROS_CONFIG_DIR": str(Path(tmp.name) / "loops.d")})
        env.start()
        self.addCleanup(env.stop)
        self.db = Path(tmp.name) / "ledger.sqlite"
        self.sup = Supervisor(self.db)
        self.loop = {**LOOP, "state_dir": str(Path(tmp.name) / "state"),
                     "unattended_fixer_push": True}

    def queue(self, turn_key=""):
        self.sup.enqueue("fix", "o/r", 3, A, "fixer", turn_key=turn_key)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='pending', push_admitted=1 WHERE delivery='fix'")
        self.sup.production_config = Path(self.db).parent / "unused-config"

    def claim(self, merges):
        pull = {"number": 3, "state": "open", "draft": False,
                "base": {"ref": "main", "sha": BASE, "repo": {"full_name": "o/r"}},
                "head": {"sha": B, "repo": {"full_name": "o/r"}}}
        with patch("diaktoros.config.by_repo", return_value=self.loop), \
             patch("diaktoros.gh.api", return_value=pull), \
             patch("diaktoros.gh.reviews", return_value=[review(1, "CHANGES_REQUESTED", A)]), \
             patch.object(merge_only, "source", side_effect=sources(merges)):
            return self.sup._claim()

    def test_the_round_moves_to_the_merge_only_head_and_is_claimed_there(self):
        self.queue()
        self.assertIsNotNone(self.claim({B: A}))
        row = self.sup.get("fix")
        self.assertEqual((row["state"], row["head"]), ("claimed", B))

    def test_a_head_that_is_not_merge_only_still_supersedes_the_round(self):
        self.queue()
        self.assertIsNone(self.claim({}))
        row = self.sup.get("fix")
        self.assertEqual((row["state"], row["error"], row["head"]),
                         ("cancelled", "fixer verdict superseded", A))

    def test_a_ci_fix_turn_does_not_follow(self):
        from diaktoros import ci_fix
        self.queue(turn_key=f"{ci_fix.KEY}{A}")
        self.assertIsNone(self.claim({B: A}))
        self.assertEqual(self.sup.get("fix")["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
