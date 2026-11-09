"""#1683: the shadow hold judges the active reviewer turn, not just the head.

On a same-head re-review (``review --reconsider``) there are two reviewer turns at one head. A
shadow review queued with the older turn never clears the newer one: until the newer turn's own
shadow has a review, the head holds. Driven through ``ready.assess_kinds`` and the merge check's
``decide`` on a real run ledger, and through ``gate.shadow_state`` for the hold's wording.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
import test_shadow_escalate_ruling as ser  # noqa: E402
from diaktoros import ci, gate, merge_check, ready  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402

APPROVED = [{"id": 9, "state": "APPROVED", "commit_id": fg.HEAD,
             "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "reviewer", "id": 2}}]
PR = {"number": 7, "draft": False, "user": {"login": "fixer"}, "base": {"ref": "main"},
      "head": {"sha": fg.HEAD, "ref": "fix-7"}}
CLEAN = {"mergeable": True, "mergeable_state": "clean", "number": 7, "state": "open",
         "draft": False, "head": {"sha": fg.HEAD}, "base": {"ref": "main"}}


class Turns(ser.Base):
    """A ledger with reviewer turns and the shadows queued with them."""

    def reviewer(self, key, at):
        """A reviewer run of turn ``key`` created at ``at``."""
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES(?,?,?,?,?,'reviewer','succeeded',0,?,?,?)",
                        (f"rev-{key}", f"drev-{key}", fg.REPO, 7, fg.HEAD, key, at, at))

    def turn_shadow(self, key, state, verdict=None, at=None):
        """A shadow run queued with reviewer turn ``key``, and its review when it has one."""
        at = time.time() if at is None else at
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES(?,?,?,?,?,'shadow',?,0,?,?,?)",
                        (f"sh-{key}", f"dsh-{key}", fg.REPO, 7, fg.HEAD, state, key, at, at))
            if verdict:
                con.execute("INSERT INTO shadow_reviews(run_id,repo,pr,head,model,verdict,body,"
                            "authorized,denial,held,created) VALUES(?,?,?,?,'m',?,'F1: a.py:1: x',"
                            "1,'',0,?)", (f"sh-{key}", fg.REPO, 7, fg.HEAD, verdict, at))

    def state(self):
        return gate.shadow_state(self.loop, 7, fg.HEAD, db=self.db)

    def kinds(self):
        """``ready``'s reasons for the head: it reads the production ledger itself."""
        return ready.assess_kinds(self.loop, self.st, PR, APPROVED, ci.CIState(), CLEAN)

    def shadow_reasons(self):
        return [text for _kind, text in self.kinds() if text.startswith("shadow review:")]

    def two_turns_old_shadow_only(self):
        now = time.time()
        self.reviewer("t1", now - 100)
        self.turn_shadow("t1", "succeeded", "APPROVE", at=now - 90)
        self.reviewer("t2", now - 10)              # the re-review: no shadow queued with it


class ActiveTurn(Turns):
    def test_an_old_turns_shadow_does_not_clear_a_newer_reviewer_turn(self):
        self.two_turns_old_shadow_only()
        kind, held, _note = self.state()
        self.assertEqual(kind, "pending")
        self.assertIn("earlier turn's does not count", held)

    def test_ready_holds_on_the_old_shadow_and_clears_when_the_new_turns_shadow_lands(self):
        self.two_turns_old_shadow_only()
        self.assertEqual(len(self.shadow_reasons()), 1, self.kinds())
        self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.PENDING)
        self.turn_shadow("t2", "succeeded", "APPROVE")
        self.assertEqual(self.shadow_reasons(), [])
        self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.SUCCESS)

    def test_a_newer_turns_shadow_at_the_head_clears_the_hold(self):
        # #1821: a dispute re-review gets its own turn key; its shadow review answers the hold.
        self.two_turns_old_shadow_only()
        self.reviewer("dispute:5", time.time() - 5)
        self.assertEqual(self.state()[0], "pending")       # no shadow of its own yet: still held
        self.turn_shadow("dispute:5", "succeeded", "APPROVE")
        self.assertEqual(self.state()[:2], ("", ""))

    def test_the_new_turns_shadow_is_waited_for_while_it_runs(self):
        self.two_turns_old_shadow_only()
        self.turn_shadow("t2", "running")
        self.assertIn("(running)", self.state()[1])
        self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.PENDING)

    def test_an_old_turns_dissent_does_not_hold_a_newer_turn_that_its_shadow_approved(self):
        now = time.time()
        self.reviewer("t1", now - 100)
        self.turn_shadow("t1", "succeeded", "REQUEST_CHANGES", at=now - 90)
        self.reviewer("t2", now - 50)
        self.turn_shadow("t2", "succeeded", "APPROVE", at=now - 40)
        self.assertEqual(self.state()[:2], ("", ""))

    def ended_without_review(self, state):
        """#1683 round 1 F1: a failed, cancelled or skipped shadow of the new turn is no second
        opinion on it, so ``ready`` and the merge check still hold."""
        self.two_turns_old_shadow_only()
        self.turn_shadow("t2", state)
        kind, held, _note = self.state()
        self.assertEqual(kind, "pending")
        self.assertIn(f"ended {state} without a review", held)
        self.assertEqual(len(self.shadow_reasons()), 1, self.kinds())
        self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.PENDING)

    def test_a_new_turn_whose_shadow_failed_without_a_review_still_holds(self):
        self.ended_without_review("failed")

    def test_a_new_turn_whose_shadow_was_cancelled_without_a_review_still_holds(self):
        self.ended_without_review("cancelled")

    def test_a_new_turn_whose_shadow_was_skipped_without_a_review_still_holds(self):
        self.ended_without_review("skipped")

    def test_a_single_turn_whose_shadow_ended_without_a_review_is_a_note_not_a_hold(self):
        self.reviewer("t1", time.time() - 100)
        self.turn_shadow("t1", "failed")
        held, note = gate.shadow_hold(self.loop, 7, fg.HEAD, db=self.db)
        self.assertEqual(held, "")
        self.assertIn("ended failed without a review", note)

    def test_a_single_turn_with_its_shadow_is_unchanged(self):
        now = time.time()
        self.reviewer("t1", now - 100)
        self.turn_shadow("t1", "succeeded", "APPROVE", at=now - 90)
        self.assertEqual(self.state()[:2], ("", ""))

    def test_a_head_with_no_shadow_at_all_is_still_nothing_to_wait_for(self):
        self.reviewer("t1", time.time() - 100)       # sampled out or queued before escalate
        kind, held, note = self.state()
        self.assertEqual((kind, held), ("", ""))
        self.assertIn("no shadow review was queued", note)

    def test_the_adjudicator_is_shown_the_active_turns_review(self):
        self.two_turns_old_shadow_only()
        self.assertIsNone(turn_prompts.newest_shadow_review(
            {"repo": fg.REPO, "pr": 7, "head": fg.HEAD}))
        self.turn_shadow("t2", "succeeded", "REQUEST_CHANGES")
        found = turn_prompts.newest_shadow_review({"repo": fg.REPO, "pr": 7, "head": fg.HEAD})
        self.assertEqual((found[0], found[2]), ("sh-t2", "REQUEST_CHANGES"))


if __name__ == "__main__":
    unittest.main()
