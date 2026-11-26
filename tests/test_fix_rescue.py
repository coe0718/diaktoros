"""An owed fix whose turn a head move ended is queued again; ``hermes dk unstick`` (#2759).

On 2026-11-26 #2738, #2745 and #2756 each had a change request standing at their head, carried
over base merges (#2757), and nothing would ever answer it: the fix turns had ended when the merges
moved the head (cancelled as superseded, or stopped at staging by the stale head), ``retry``
re-arms no cancelled turn, and a redelivered verdict names the old head. The sweep now queues the
owed fix itself, and ``unstick`` takes the step a stuck PR is owed on demand.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_fixer_gating as fg  # noqa: E402
import test_merge_only_heads as tmo  # noqa: E402
import test_review_follows_head as rfh  # noqa: E402
from diaktoros import config, fix_rescue, gh, merge_only, pr_hold  # noqa: E402
from diaktoros import retry_action, review_kick, run_supervisor, unstick  # noqa: E402
from diaktoros import util  # noqa: E402

OLD = rfh.OLD
SUPERSEDED = run_supervisor.FIX_VERDICT_SUPERSEDED
STALE = "isolated turn failed: FetchDenied: stale PR head"
TIMEOUT = "isolated turn failed: TimeoutExpired — killed at the 1800s turn budget"


def asked(at=None):
    """The change request at ``OLD``, submitted ``at`` (default: an hour ago)."""
    when = util.iso_at(time.time() - 3600 if at is None else at)
    return {**fg.verdict(), "commit_id": OLD, "submitted_at": when}


def merged_from_old(loop, head):
    """``fg.HEAD`` only merged the base into ``OLD``."""
    return OLD if head == fg.HEAD else ""


class Ledger(fg.Base):
    """A real run ledger where the host keeps it (``config.host_path("ledger")``)."""

    def setUp(self):
        super().setUp()
        self.set_push(True)
        self.db = config.host_path("ledger")
        self.db.parent.mkdir(parents=True, exist_ok=True)
        run_supervisor.Supervisor(self.db)

    def turn(self, state, error="", head=OLD, seat="fixer", key="", created=None):
        row = rfh.queue(self.db, state, seat=seat, head=head)
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET error=?, turn_key=?, created=? WHERE id=?",
                        (error, key, time.time() if created is None else created, row["id"]))


class Owed(Ledger):
    def owed(self, review=None, lookup=merged_from_old):
        with mock.patch.object(merge_only, "lookup", side_effect=lookup):
            return fix_rescue.owed(self.loop, self.db, 7, fg.HEAD, review or asked())

    def test_a_turn_a_head_move_ended_is_owed_at_the_merged_head(self):
        # #2738 / #2745: cancelled at the claim; #2756: stopped at staging by the stale head.
        for error, state in ((SUPERSEDED, "cancelled"), (STALE, "failed")):
            with self.subTest(state=state):
                self.setUp()
                self.turn(state, error)
                self.assertIs(self.owed(), True)

    def test_a_turn_that_ran_and_ended_is_not_owed_again(self):
        # A timeout, or an answer that pushed nothing: retry is the operator's, not each sweep's.
        for state, error in (("failed", TIMEOUT), ("succeeded", ""), ("cancelled", "by operator")):
            with self.subTest(state=state, error=error):
                self.setUp()
                self.turn(state, error)
                self.assertIs(self.owed(), False)

    def test_a_turn_at_the_head_already_answers(self):
        for state in ("pending", "running", "failed", "cancelled"):
            with self.subTest(state=state):
                self.setUp()
                self.turn("cancelled", SUPERSEDED)
                self.turn(state, SUPERSEDED, head=fg.HEAD)
                self.assertIs(self.owed(), False)

    def test_a_turn_at_the_head_answers_without_a_merge_only_read(self):
        self.turn("cancelled", SUPERSEDED, head=fg.HEAD)
        with mock.patch.object(merge_only, "lookup") as lookup:
            self.assertIs(fix_rescue.owed(self.loop, self.db, 7, fg.HEAD, asked()), False)
        lookup.assert_not_called()

    def test_only_a_head_reached_by_base_merges_alone_owes_it(self):
        self.turn("cancelled", SUPERSEDED)
        self.assertIs(self.owed(lookup=lambda loop, head: ""), False)        # a real push since
        self.assertIsNone(self.owed(lookup=lambda loop, head: merge_only.UNKNOWN))
        far = tmo.lookups(tmo.chain(fg.HEAD, OLD, 3))
        self.assertIs(self.owed(lookup=far), True)                             # through a chain

    def test_a_verdict_no_turn_was_queued_for_is_owed(self):
        # A webhook that queued nothing; a turn for an earlier verdict does not answer this one.
        self.turn("succeeded", created=time.time() - 7200)
        self.assertIs(self.owed(), True)

    def test_other_fix_turns_answer_no_verdict(self):
        # A CI-fix or conflict turn at the head is not the verdict's answer.
        self.turn("cancelled", SUPERSEDED)
        self.turn("succeeded", head=fg.HEAD, key="cifix:")
        self.assertIs(self.owed(), True)

    def test_an_unreadable_ledger_or_an_undated_verdict_decides_nothing(self):
        self.turn("cancelled", SUPERSEDED)
        self.assertIs(self.owed({**asked(), "submitted_at": None}), False)
        self.db.unlink()
        self.assertIsNone(self.owed())


class SweepQueuesIt(Ledger):
    """The watchdog's sweep, driven as ``test_review_follows_head.SweepDeliversIt`` drives it."""

    sweep = rfh.SweepDeliversIt.sweep
    arm = rfh.SweepDeliversIt.arm
    observed_long_ago = rfh.SweepDeliversIt.observed_long_ago

    def setUp(self):
        super().setUp()
        self.kicks, self.moved, self.queued = [], None, []

    def sweep_fix(self, reviews, grace_s=None):
        def queue(loop, number, head):
            self.queued.append((number, head))
            self.turn("pending", head=head)       # the gate's key: the next sweep sees it
            return "enqueued"
        with mock.patch.object(merge_only, "lookup", side_effect=merged_from_old), \
                mock.patch.object(fix_rescue, "queue", side_effect=queue):
            return self.sweep(reviews, grace_s=grace_s)

    def test_the_fix_a_head_move_ended_is_queued_at_the_merged_head_once(self):
        self.arm()
        self.turn("cancelled", SUPERSEDED)
        self.sweep_fix([asked()])
        self.assertEqual(self.queued, [(7, fg.HEAD)])
        self.sweep_fix([asked()])
        self.assertEqual(self.queued, [(7, fg.HEAD)], "once: the turn at the head answers it")

    def test_a_fix_turn_that_failed_on_its_own_is_left_to_the_operator(self):
        self.arm()
        self.turn("failed", TIMEOUT)
        out = self.sweep_fix([asked()])
        self.assertEqual(self.queued, [])
        self.assertIn("fixer never pushed", out)

    def test_it_waits_until_the_verdict_and_the_head_have_stood_a_sweep_interval(self):
        # Real clocks: a verdict a minute old, or a head just seen, is the gate's still.
        self.arm(grace_s=36000)
        self.turn("cancelled", SUPERSEDED)
        self.sweep_fix([asked()], grace_s=36000)                  # the head was just seen
        self.observed_long_ago()
        self.sweep_fix([asked(at=time.time() - 60)], grace_s=36000)
        self.assertEqual(self.queued, [])
        self.sweep_fix([asked()], grace_s=36000)
        self.assertEqual(self.queued, [(7, fg.HEAD)])


class Unstick(Ledger):
    """``hermes dk unstick``: the step owed, through the real decision, with GitHub faked."""

    def unstick(self, reviews, *, dry_run=False, pr=None, lookup=merged_from_old, hold=(None, "")):
        self.queued, self.kicked, self.retried = [], [], []

        def queue(loop, number, head):
            self.queued.append((number, head))
            return "enqueued"

        def kick(loop, pr, head, sender, tag):
            self.kicked.append((pr["number"], head, tag))
            return True

        def retry(loop, number, seat, **kw):
            self.retried.append((number, seat, kw["resolve_head"]()))
            return {"ok": True, "message": "", "refused": [],
                    "rearmed": [{"seat": seat, "state": "failed", "error": "x"}]}
        with mock.patch.object(gh, "pr", return_value=pr or fg.LIVE), \
                mock.patch.object(gh, "reviews", return_value=reviews), \
                mock.patch.object(pr_hold, "read", return_value=hold), \
                mock.patch.object(merge_only, "lookup", side_effect=lookup), \
                mock.patch.object(fix_rescue, "queue", side_effect=queue), \
                mock.patch.object(review_kick, "kick", side_effect=kick), \
                mock.patch.object(retry_action, "perform", side_effect=retry):
            return unstick.perform(self.loop, 7, dry_run=dry_run)

    def test_a_carried_change_request_with_no_fix_turn_gets_one(self):
        self.turn("cancelled", SUPERSEDED)
        out = self.unstick([asked()])
        self.assertEqual((out["code"], self.queued), (0, [(7, fg.HEAD)]))
        self.assertIn("fixer enqueued", out["lines"][0])

    def test_a_dry_run_says_the_step_and_takes_none(self):
        out = self.unstick([asked()], dry_run=True)
        self.assertEqual((out["code"], self.queued), (0, []))
        self.assertIn(f"would {unstick.FIX}", out["lines"][0])

    def test_a_fix_turn_at_the_head_that_ended_is_re_armed(self):
        self.turn("failed", TIMEOUT, head=fg.HEAD)
        out = self.unstick([fg.verdict()])
        self.assertEqual((out["code"], self.retried, self.queued), (0, [(7, "fixer", fg.HEAD)], []))

    def test_a_turn_already_out_is_left_alone(self):
        self.turn("pending", head=fg.HEAD)
        out = self.unstick([fg.verdict()])
        self.assertEqual((out["code"], self.queued, self.retried), (1, [], []))
        self.assertIn("already pending", out["lines"][0])

    def test_no_verdict_delivers_the_review_request_or_re_arms_the_review(self):
        out = self.unstick([])
        self.assertEqual((out["code"], self.kicked), (0, [(7, fg.HEAD, "unstick-review-7")]))
        self.turn("failed", TIMEOUT, head=fg.HEAD, seat="reviewer")
        out = self.unstick([])
        self.assertEqual((out["code"], self.retried, self.kicked), (0, [(7, "reviewer", fg.HEAD)], []))

    def test_what_is_not_the_loops_to_do_is_said_and_left(self):
        approved = [fg.verdict(6, "APPROVED")]
        capped = [{**fg.verdict(i), "submitted_at": f"2026-01-01T00:0{i}:00Z"} for i in (1, 2, 3)]
        for reviews, why in ((approved, "approved"), (capped, "3/3 verdicts spent")):
            with self.subTest(why=why):
                out = self.unstick(reviews)
                self.assertEqual((out["code"], self.queued, self.kicked, self.retried),
                                 (1, [], [], []))
                self.assertIn(why, out["lines"][0])
        out = self.unstick([fg.verdict()], hold=({"by": "coe0718"}, ""))
        self.assertEqual((out["code"], self.queued), (1, []))
        self.assertIn("holds it", out["lines"][0])
        self.set_push(False)
        out = self.unstick([fg.verdict()])
        self.assertEqual((out["code"], self.queued), (1, []))
        self.assertIn("fixer pushes are off", out["lines"][0])

    def test_an_unread_pr_or_an_undecided_carry_does_nothing(self):
        out = self.unstick([asked()], lookup=lambda loop, head: merge_only.UNKNOWN)
        self.assertEqual((out["code"], self.queued, self.kicked), (2, [], []))
        self.assertIn(merge_only.UNDECIDED_WHY, out["lines"][0])
        out = self.unstick([asked()], hold=(None, "events unreadable"))
        self.assertEqual((out["code"], self.queued), (2, []))
        with mock.patch.object(gh, "pr", return_value=None):
            self.assertEqual(unstick.perform(self.loop, 7)["code"], 2)


if __name__ == "__main__":
    unittest.main()
