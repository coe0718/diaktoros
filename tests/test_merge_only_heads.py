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
ON_BASE = {"status": "ahead", "behind_by": 0}      # the merged parent is an ancestor of main


def review(rid, state, head, minute=0):
    return {"id": rid, "state": state, "commit_id": head, "user": {"login": "review"},
            "submitted_at": f"2026-01-01T00:{minute:02d}:00Z"}


def fresh(test):
    """Empty ``merge_only``'s per-process cache of a head's own facts for ``test``."""
    merge_only._MEMO.clear()
    test.addCleanup(merge_only._MEMO.clear)


def flaky(api, failures=1):
    """``api`` with its first ``failures`` commit reads failing, as a transient GitHub error does."""
    left = [failures]

    def call(loop, path, *a, **k):
        if "/commits/" in path and left[0] > 0:
            left[0] -= 1
            return None
        return api(loop, path, *a, **k)
    return call


def sources(mapping):
    """A fake ``merge_only.source``: each head's merge-only parent, from ``mapping``."""
    return lambda loop, head: mapping.get(head)


def lookups(mapping):
    """A fake ``merge_only.lookup``: ``""`` (not merge-only) for any head not in ``mapping``."""
    return lambda loop, head: mapping.get(head, "")


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
        fresh(self)

    def merge(self, before=PR_FILES, after=PR_FILES, base=None):
        commits = {B: {"parents": [{"sha": A}, {"sha": BASE}]}}
        compares = {f"{BASE}...{A}": {"files": before}, f"{BASE}...{B}": {"files": after},
                    f"{BASE}...main": ON_BASE if base is None else base}
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

    def test_whether_the_parent_is_on_the_base_is_read_afresh_each_time(self):
        # #2757 review F2: the base branch moves, so its answer is never taken from the cache.
        with patch("diaktoros.gh.api", side_effect=self.merge()):
            self.assertEqual(merge_only.lookup(LOOP, B), A)
        paths = []
        rewritten = self.merge(base={"status": "diverged", "behind_by": 3})

        def api(loop, path, *a, **k):
            paths.append(path)
            return rewritten(loop, path, *a, **k)
        with patch("diaktoros.gh.api", side_effect=api):
            self.assertEqual(merge_only.lookup(LOOP, B), "")
        self.assertEqual(paths, [f"/repos/o/r/compare/{BASE}...main"], "only the base is re-read")

    def test_one_answer_stands_for_a_decision_and_is_read_again_after_it(self):
        # #2757 review rounds 3 and 4, F5: two asks inside one decision get the same answer,
        # however long it runs and even when GitHub recovers between them; the next reads again.
        with patch("diaktoros.gh.api", side_effect=flaky(self.merge())):
            with merge_only.decision():
                self.assertEqual(merge_only.lookup(LOOP, B), merge_only.UNKNOWN)
                with merge_only.decision():         # a decision inside it is part of it
                    self.assertEqual(merge_only.lookup(LOOP, B), merge_only.UNKNOWN)
            with merge_only.decision():
                self.assertEqual(merge_only.lookup(LOOP, B), A)

    def test_no_answer_outlives_its_decision(self):
        # #2757 review round 4, F2: a positive answer never stands for a later decision, so a
        # base rewritten since is read as it is now.
        with patch("diaktoros.gh.api", side_effect=self.merge()), merge_only.decision():
            self.assertEqual(merge_only.lookup(LOOP, B), A)
        rewritten = self.merge(base={"status": "diverged", "behind_by": 3})
        with patch("diaktoros.gh.api", side_effect=rewritten), merge_only.decision():
            self.assertEqual(merge_only.lookup(LOOP, B), "")

    def test_an_answer_for_one_base_is_never_one_for_another(self):
        # #2757 review round 4, F2: the same head under a loop whose base is another branch.
        commits = {B: {"parents": [{"sha": A}, {"sha": BASE}]}}
        compares = {f"{BASE}...{A}": {"files": PR_FILES}, f"{BASE}...{B}": {"files": PR_FILES},
                    f"{BASE}...main": ON_BASE, f"{BASE}...release": {"status": "diverged",
                                                                     "behind_by": 4}}
        with patch("diaktoros.gh.api", side_effect=github(commits, compares)), \
                merge_only.decision():
            self.assertEqual(merge_only.lookup(LOOP, B), A)
            self.assertEqual(merge_only.lookup({**LOOP, "base": "release"}, B), "")

    def test_a_diff_too_large_to_list_is_a_definite_no_not_unknown(self):
        # #2757 review F4: the same 300-file answer comes back on every retry.
        many = [{"filename": f"f{i}.py", "status": "modified", "sha": "1" * 40}
                for i in range(merge_only.FILES_MAX)]
        with patch("diaktoros.gh.api", side_effect=self.merge(before=many, after=many)):
            self.assertEqual(merge_only.lookup(LOOP, B), "")

    def test_a_merge_of_a_branch_that_is_not_the_base_is_not(self):
        # #2757 review F2: a feature branch's new file is in neither three-dot diff, so the diff
        # equality alone would pass it; the merged parent must be on the loop's base.
        for answer in ({"status": "diverged", "behind_by": 2}, {"status": "behind", "behind_by": 1}):
            with self.subTest(answer=answer), \
                    patch("diaktoros.gh.api", side_effect=self.merge(base=answer)):
                self.assertEqual(merge_only.lookup(LOOP, B), "")

    def test_an_unreadable_read_is_unknown_and_is_read_again(self):
        commits = {B: {"parents": [{"sha": A}, {"sha": BASE}]}}
        for compares in ({f"{BASE}...main": None},
                         {f"{BASE}...main": ON_BASE, f"{BASE}...{A}": None}):
            with self.subTest(compares=list(compares)), \
                    patch("diaktoros.gh.api", side_effect=github(commits, compares)):
                self.assertEqual(merge_only.lookup(LOOP, B), merge_only.UNKNOWN)
                self.assertIsNone(merge_only.source(LOOP, B))
        with patch("diaktoros.gh.api", side_effect=self.merge()):
            self.assertEqual(merge_only.source(LOOP, B), A)

    def test_a_chain_of_merges_leads_back_to_the_reviewed_head(self):
        with patch.object(merge_only, "lookup", side_effect=lookups({C: B, B: A})):
            self.assertTrue(merge_only.leads_to(LOOP, C, A))
            self.assertFalse(merge_only.leads_to(LOOP, C, "d" * 40))
        with patch.object(merge_only, "lookup", side_effect=lookups({C: merge_only.UNKNOWN})):
            self.assertIsNone(merge_only.leads_to(LOOP, C, A))


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


class ConflictNotice(unittest.TestCase):
    def test_an_undecided_carry_queues_no_merge_and_says_a_later_sweep_reads_again(self):
        # Review of #2757, F7: not "the fixer answers first": nothing decided a change request.
        from diaktoros import watch_conflicts
        with patch.object(watch_conflicts, "fix_owed", return_value=watch_conflicts.UNDECIDED), \
                patch.object(gate, "enqueue_isolated") as queued:
            notice = watch_conflicts._queue_turn(LOOP, None, 3, B, BASE)
        queued.assert_not_called()
        self.assertIn("a later sweep re-reads", notice)


class FixerClaim(unittest.TestCase):
    """The fixer's answer turn, queued at the reviewed head, runs at the merge-only head."""

    def setUp(self):
        fresh(self)
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
             patch.object(merge_only, "lookup", side_effect=lookups(merges)), \
             patch.object(merge_only, "source", side_effect=sources(
                 {k: v for k, v in merges.items() if v != merge_only.UNKNOWN})):
            return self.sup._claim()

    def there(self, state):
        """A same-key round already at the merge-only head, in ``state``."""
        self.sup.enqueue("there", "o/r", 3, B, "fixer")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state=?, push_admitted=1, error='earlier' "
                        "WHERE delivery='there'", (state,))

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

    def test_an_unreadable_merge_check_backs_off_and_the_round_moves_once_it_reads(self):
        # #2757 review F1: a failed read used to cancel the only answer for good.
        self.queue()
        self.assertIsNone(self.claim({B: merge_only.UNKNOWN}))
        row = self.sup.get("fix")
        self.assertEqual((row["state"], row["head"]), ("waiting", A))
        self.assertIn("merge-only check unreadable", row["error"])
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='pending', retry_at=NULL WHERE delivery='fix'")
        self.assertIsNotNone(self.claim({B: A}))
        self.assertEqual(self.sup.get("fix")["head"], B)

    def test_a_cancelled_round_already_at_the_head_is_rearmed_to_answer(self):
        # #2757 review F3: a terminal row there used to retire the only live answer too.
        self.queue()
        self.there("cancelled")
        self.assertIsNotNone(self.claim({B: A}))
        self.assertEqual(self.sup.get("there")["state"], "claimed")
        self.assertEqual(self.sup.get("fix")["state"], "cancelled")

    def test_a_round_already_answering_there_is_left_to_answer(self):
        for state in ("pending", "succeeded"):
            with self.subTest(state=state):
                self.setUp()
                self.queue()
                self.there(state)
                self.claim({B: A})
                self.assertEqual(self.sup.get("fix")["state"], "cancelled")
                self.assertIn(self.sup.get("there")["state"], (state, "claimed"))

    def test_an_oversized_merge_falls_back_through_the_real_claim(self):
        # #2757 review F4, end to end: not merge-only, so the round is superseded as before and
        # the head is reviewed as usual, never left retrying a read that cannot shrink.
        self.queue()
        many = [{"filename": f"f{i}.py", "status": "modified", "sha": "1" * 40}
                for i in range(merge_only.FILES_MAX)]
        fake = github({B: {"parents": [{"sha": A}, {"sha": BASE}]}},
                      {f"{BASE}...main": ON_BASE, f"{BASE}...{A}": {"files": many},
                       f"{BASE}...{B}": {"files": many}})
        pull = {"number": 3, "state": "open", "draft": False,
                "base": {"ref": "main", "sha": BASE, "repo": {"full_name": "o/r"}},
                "head": {"sha": B, "repo": {"full_name": "o/r"}}}

        def api(loop, path, *a, **k):
            return fake(loop, path) if ("/commits/" in path or "/compare/" in path) else pull
        with patch("diaktoros.config.by_repo", return_value=self.loop), \
             patch("diaktoros.gh.api", side_effect=api), \
             patch("diaktoros.gh.reviews", return_value=[review(1, "CHANGES_REQUESTED", A)]):
            self.assertIsNone(self.sup._claim())
        self.assertEqual(self.sup.get("fix")["error"], "fixer verdict superseded")

    def test_a_ci_fix_turn_does_not_follow(self):
        from diaktoros import ci_fix
        self.queue(turn_key=f"{ci_fix.KEY}{A}")
        self.assertIsNone(self.claim({B: A}))
        self.assertEqual(self.sup.get("fix")["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()


class ReviewerClaim(FixerClaim):
    """A review queued at a merge-only head whose change request stands there (#2757 review F5)."""

    def read(self, merges):
        from diaktoros.run_supervisor import turns
        self.sup.enqueue("review", "o/r", 3, B, "reviewer")
        with self.sup._connect() as con:
            row = con.execute("SELECT * FROM runs WHERE delivery='review'").fetchone()
        pull = {"number": 3, "state": "open", "draft": False,
                "base": {"ref": "main", "sha": BASE, "repo": {"full_name": "o/r"}},
                "head": {"sha": B, "repo": {"full_name": "o/r"}}}
        read = turns._ClaimRead()
        with patch("diaktoros.config.by_repo", return_value=self.loop), \
             patch("diaktoros.gh.api", return_value=pull), \
             patch("diaktoros.review_receipt.generation_for", return_value="g"), \
             patch("diaktoros.gh.reviews", return_value=[review(1, "CHANGES_REQUESTED", A)]), \
             patch.object(merge_only, "lookup", side_effect=lookups(merges)):
            self.sup._claim_read_reviewer(row, read)
        return read

    def test_a_carried_change_request_answers_a_queued_review(self):
        read = self.read({B: A})
        self.assertEqual(read.superseded, "superseded: reviewed at this head by review")

    def test_an_undecided_carry_waits_instead_of_reviewing(self):
        read = self.read({B: merge_only.UNKNOWN})
        self.assertIsNone(read.superseded)
        self.assertIn("merge-only check unreadable", read.read_error)

    def test_a_head_that_is_not_merge_only_is_reviewed(self):
        read = self.read({})
        self.assertEqual((read.superseded, read.read_error), (None, ""))


class ReviewerClaimRecovering(FixerClaim):
    """A read that fails, then recovers, around one reviewer claim, driven through the real
    ``_claim`` (#2757 review rounds 3 and 4, F5)."""

    def claim_with(self, api):
        self.sup.enqueue("review", "o/r", 3, B, "reviewer")
        with self.sup._connect() as con:
            con.execute("UPDATE runs SET state='pending' WHERE delivery='review'")
        self.sup.production_config = Path(self.db).parent / "unused-config"
        pull = {"number": 3, "state": "open", "draft": False,
                "base": {"ref": "main", "sha": BASE, "repo": {"full_name": "o/r"}},
                "head": {"sha": B, "repo": {"full_name": "o/r"}}}

        def answer(loop, path, *a, **k):
            return api(loop, path) if ("/commits/" in path or "/compare/" in path) else pull
        with patch("diaktoros.config.by_repo", return_value=self.loop), \
             patch("diaktoros.gh.api", side_effect=answer), \
             patch("diaktoros.review_receipt.generation_for", return_value="g"), \
             patch("diaktoros.gh.reviews", return_value=[review(1, "CHANGES_REQUESTED", A)]):
            self.sup._claim()
        row = self.sup.get("review")
        with self.sup._connect() as con:
            con.execute("DELETE FROM runs WHERE delivery='review'")
        return row

    def merge(self):
        return github({B: {"parents": [{"sha": A}, {"sha": BASE}]}},
                      {f"{BASE}...main": ON_BASE, f"{BASE}...{A}": {"files": PR_FILES},
                       f"{BASE}...{B}": {"files": PR_FILES}})

    def test_a_read_that_recovers_mid_claim_still_waits(self):
        row = self.claim_with(flaky(self.merge()))
        self.assertEqual(row["state"], "waiting")
        self.assertIn("merge-only check unreadable", row["error"])

    def test_an_earlier_unknown_never_decides_a_later_claim(self):
        # Review round 4: an UNKNOWN read just before the claim began, by the sweep or another
        # claim, is not this claim's answer; nor does one expire in the middle of it.
        with patch("diaktoros.gh.api", side_effect=flaky(self.merge())), merge_only.decision():
            self.assertEqual(merge_only.lookup(self.loop, B), merge_only.UNKNOWN)
        row = self.claim_with(self.merge())
        self.assertEqual((row["state"], row["error"]),
                         ("cancelled", "superseded: reviewed at this head by review"))

    def test_the_next_claim_reads_again_and_the_carry_answers(self):
        self.claim_with(flaky(self.merge()))
        row = self.claim_with(self.merge())
        self.assertEqual(row["error"], "superseded: reviewed at this head by review")
