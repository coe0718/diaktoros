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
from diaktoros import config, fix_rescue, gate, gh, merge_check, merge_only  # noqa: E402
from diaktoros import observer, one_shot_action, pr_hold, retry_action, review_kick  # noqa: E402
from diaktoros import run_supervisor, transition, unstick, util  # noqa: E402
from diaktoros.run_supervisor import views  # noqa: E402

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

    def turn(self, state, error="", head=OLD, seat="fixer", key="", created=None, updated=None,
             retries=0):
        """One turn of PR 7 in ``state``; its run id."""
        self.n = getattr(self, "n", 0) + 1
        run_id, now = f"r{self.n}", time.time()
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs (id, delivery, repo, pr, head, seat, state, attempts, "
                        "error, created, updated, turn_key, push_admitted, retries) "
                        "VALUES (?, ?, ?, 7, ?, ?, ?, 0, ?, ?, ?, ?, 1, ?)",
                        (run_id, f"d{run_id}", fg.REPO, head, seat, state, error,
                         now if created is None else created, now if updated is None else updated,
                         key, retries))
        return run_id

    def state(self, run_id):
        return rfh.read_row(self.db, run_id)["state"]


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

    def test_a_fix_turn_of_another_key_out_at_the_head_is_waited_for(self):
        # Review of #2760, F12: no plain turn beside a CI fix or conflict turn still out there.
        for key in ("cifix:", "conflict:"):
            for state in ("pending", "running"):
                with self.subTest(key=key, state=state):
                    self.setUp()
                    self.turn("cancelled", SUPERSEDED)
                    self.turn(state, head=fg.HEAD, key=key)
                    self.assertIs(self.owed(), False)

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

    def test_a_fix_turn_of_another_key_running_at_the_head_is_waited_for(self):
        # Review of #2760, F12: a CI fix running at the head gets no plain turn beside it; once
        # it has ended, the change request's own turn is the one owed.
        self.arm()
        self.turn("cancelled", SUPERSEDED)
        cifix = self.turn("running", head=fg.HEAD, key="cifix:")
        self.sweep_fix([asked()])
        self.assertEqual(self.queued, [])
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state='succeeded' WHERE id=?", (cifix,))
        self.sweep_fix([asked()])
        self.assertEqual(self.queued, [(7, fg.HEAD)])

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

    def unstick(self, reviews, *, dry_run=False, pr=None, lookup=merged_from_old, hold=(None, ""),
                kick=None):
        self.queued, self.kicked = [], []

        def queue(loop, number, head):
            self.queued.append((number, head))
            return "enqueued"

        def kicked(loop, pr, head, sender, tag):
            self.kicked.append((pr["number"], head, tag))
            return kick(loop, pr, head, sender) if kick else True
        # The real retry re-arms; only its worker start is stubbed.
        with mock.patch.object(gh, "pr", return_value=pr or fg.LIVE), \
                mock.patch.object(gh, "reviews", return_value=reviews), \
                mock.patch.object(pr_hold, "read", return_value=hold), \
                mock.patch.object(merge_only, "lookup", side_effect=lookup), \
                mock.patch.object(fix_rescue, "queue", side_effect=queue), \
                mock.patch.object(review_kick, "kick", side_effect=kicked), \
                mock.patch.object(retry_action.gate, "resume_isolated", return_value=True):
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

    def test_a_fix_turn_at_the_head_that_ended_is_re_armed_and_no_other(self):
        # Review of #2760, F1 and F2: a failed turn at an older head, updated since, and a failed
        # CI-fix turn at this head, are not the change request's answer and stay as they were.
        here = self.turn("failed", TIMEOUT, head=fg.HEAD, updated=time.time() - 600)
        older = self.turn("failed", TIMEOUT)
        cifix = self.turn("failed", TIMEOUT, head=fg.HEAD, key="cifix:")
        out = self.unstick([fg.verdict()])
        self.assertEqual(out["code"], 0, out["lines"])
        self.assertEqual([self.state(i) for i in (here, older, cifix)],
                         ["pending", "failed", "failed"])
        self.assertEqual(self.queued, [])
        self.assertIn(f"@ {fg.HEAD[:7]}: fixer re-armed (was failed", out["lines"][0])

    def test_a_turn_already_out_is_left_alone(self):
        self.turn("pending", head=fg.HEAD)
        out = self.unstick([fg.verdict()])
        self.assertEqual((out["code"], self.queued), (1, []))
        self.assertIn("already pending", out["lines"][0])

    def test_a_fix_turn_of_any_key_already_out_is_left_alone(self):
        # Review of #2760, F8: a CI fix running at the head, and the change request's own turn
        # not queued yet. Once the CI fix has ended it answers no verdict: the plain one is owed.
        cifix = self.turn("running", head=fg.HEAD, key="cifix:")
        out = self.unstick([fg.verdict()])
        self.assertEqual((out["code"], self.queued), (1, []))
        self.assertIn("fixer turn is already running", out["lines"][0])
        with contextlib.closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state='succeeded' WHERE id=?", (cifix,))
        out = self.unstick([fg.verdict()])
        self.assertEqual((out["code"], self.queued), (0, [(7, fg.HEAD)]), out["lines"])

    def gate(self, reviews):
        """The review request delivered to the real reviewer gate, in process, as the host's
        ``review`` calls it; its ledger is this one, and no worker starts."""
        supervisor = run_supervisor.Supervisor(self.db, fixture_mode=True, fixture_command=["true"])

        def deliver(loop, pr, head, sender):
            with mock.patch.object(gate, "isolated_supervisor", return_value=supervisor), \
                    mock.patch.object(gate, "fetch_reviews", return_value=reviews), \
                    mock.patch.object(gate, "drain_seat"), \
                    mock.patch.object(merge_check, "mark_pending"), \
                    mock.patch.object(observer, "notify"), \
                    mock.patch.object(run_supervisor.Supervisor, "_spawn"):
                self.logged = one_shot_action._run_reviewer(
                    review_kick.review_requested(loop, pr, head, sender))
            return True
        return deliver

    def keys(self):
        with contextlib.closing(sqlite3.connect(self.db)) as con:
            return dict(con.execute("SELECT turn_key, state FROM runs WHERE seat='reviewer'"))

    def test_a_dismissed_verdict_gets_the_review_the_gate_queues_for_it(self):
        # Review of #2760, F4: the reviewer's change request at the head was dismissed, and the
        # head's plain review turn already succeeded. The turn owed is the dismissal's; delivered
        # through the real gate, it is queued under ``dismissed:42`` and the plain one is left.
        reviews = [{**fg.verdict(42), "state": "DISMISSED"}]
        self.turn("succeeded", head=fg.HEAD, seat="reviewer")
        self.assertIn(f"would {unstick.REVIEW}", self.unstick(reviews, dry_run=True)["lines"][0])
        out = self.unstick(reviews, kick=self.gate(reviews))
        self.assertEqual(out["code"], 0, out["lines"])
        self.assertEqual(self.kicked, [(7, fg.HEAD, "unstick-review-7")])
        self.assertEqual(self.keys(), {"": "succeeded", "dismissed:42": "pending"})
        # Again: that turn is out now, and nothing more is delivered.
        self.assertEqual((self.unstick(reviews)["code"], self.kicked), (1, []))

    def test_a_review_turn_redeliveries_no_longer_re_arm_is_re_armed(self):
        # Review of #2760, F6: past MAX_REARMS failures the gate's intake refuses a redelivery;
        # unstick re-arms that turn itself, as ``retry`` does, and delivers nothing.
        failed = self.turn("failed", TIMEOUT, head=fg.HEAD, seat="reviewer",
                           retries=run_supervisor.MAX_REARMS)
        self.assertIn(f"would {unstick.REREVIEW}", self.unstick([], dry_run=True)["lines"][0])
        out = self.unstick([])
        self.assertEqual((out["code"], self.kicked), (0, []), out["lines"])
        self.assertEqual(self.state(failed), "pending")
        self.assertIn("reviewer re-armed (was failed", out["lines"][0])

    def test_the_gates_intake_leaves_that_turn_failed(self):
        # The F6 premise, through the real intake: delivered to the gate (as before the fix),
        # the request "succeeds" and the turn stays failed.
        failed = self.turn("failed", TIMEOUT, head=fg.HEAD, seat="reviewer",
                           retries=run_supervisor.MAX_REARMS)
        with mock.patch.object(transition, "unrecorded", return_value=True):
            out = self.unstick([], kick=self.gate([]))
        self.assertEqual((out["code"], self.state(failed)), (0, "failed"))
        self.assertEqual(self.kicked, [(7, fg.HEAD, "unstick-review-7")])
        self.assertIn(f"{run_supervisor.MAX_REARMS} failed attempts — only `hermes dk retry` "
                      "re-arms it", "\n".join(self.logged))

    def test_a_review_turn_retry_would_refuse_is_said_dry_and_for_real(self):
        ended = self.turn("succeeded", head=fg.HEAD, seat="reviewer")
        dry, real = self.unstick([], dry_run=True), self.unstick([])
        self.assertEqual(dry, real)
        self.assertEqual((real["code"], self.kicked, self.state(ended)), (1, [], "succeeded"))
        self.assertIn("review turn here ended succeeded, and retry would not re-arm", real["lines"][0])

    def test_a_review_turn_of_any_key_already_out_is_left_alone(self):
        # Review of #2760, F7: a running turn of another key than the one owed here (the plain
        # one), updated before the plain turn ended.
        self.turn("running", head=fg.HEAD, seat="reviewer", key="dismissed:9",
                  updated=time.time() - 600)
        self.turn("succeeded", head=fg.HEAD, seat="reviewer")
        out = self.unstick([])
        self.assertEqual((out["code"], self.kicked), (1, []))
        self.assertIn("reviewer turn is already running", out["lines"][0])

    def test_a_retargets_review_turn_is_the_one_owed(self):
        # The gate's key after a recorded retarget is the transition's; one it has still to
        # record names a turn not written yet, so the request is delivered.
        self.turn("failed", TIMEOUT, head=fg.HEAD, seat="reviewer")
        entry = self.st.transition_set(7, {"head": fg.HEAD, "from_base": "stack", "at": 1000,
                                           "observed_stacked_head": OLD, "old_review_ids": []})
        self.assertIn(f"would {unstick.REVIEW}", self.unstick([], dry_run=True)["lines"][0])
        self.turn("failed", TIMEOUT, head=fg.HEAD, seat="reviewer", key=transition.turn_key(entry))
        self.assertIn(f"would {unstick.REREVIEW}", self.unstick([], dry_run=True)["lines"][0])
        self.st.transition_set(7, {**entry, "head": "c" * 40})
        watch = self.st.watch()
        watch.setdefault("stacked_wait", {})["7"] = {"head": OLD, "base": "stack"}
        self.st.watch_save(watch)
        self.assertTrue(transition.unrecorded(self.loop, self.st, 7, fg.HEAD))
        self.assertIn(f"would {unstick.REVIEW}", self.unstick([], dry_run=True)["lines"][0])

    def test_no_verdict_from_before_a_retarget_still_to_be_recorded_counts(self):
        # Review of #2760, F9: the PR moved off a stacked base at the same head, and no gate has
        # recorded it yet. The stacked review's change request or approval is no verdict on the
        # root base: the fresh review is owed, and the reviewer gate records the boundary.
        watch = self.st.watch()
        watch.setdefault("stacked_wait", {})["7"] = {"head": OLD, "base": "stack"}
        self.st.watch_save(watch)
        for old in (fg.verdict(), fg.verdict(6, "APPROVED")):
            with self.subTest(state=old["state"]):
                out = self.unstick([old])
                self.assertEqual(out["code"], 0, out["lines"])
                self.assertEqual((self.queued, self.kicked), ([], [(7, fg.HEAD, "unstick-review-7")]))
        running = self.turn("running", head=fg.HEAD, seat="reviewer", key="retarget:stack:1")
        out = self.unstick([fg.verdict()])
        self.assertEqual((out["code"], self.queued, self.kicked), (1, [], []))
        self.assertIn("reviewer turn is already running", out["lines"][0])
        self.assertEqual(self.state(running), "running")

    def test_a_persons_approval_still_holds_across_a_retarget_still_to_be_recorded(self):
        # Review of #2760, F10: the boundary is recorded by the gate, but the person's approval
        # left at this head stands (``gate_reviewer.decide`` still reads it): nothing is owed.
        watch = self.st.watch()
        watch.setdefault("stacked_wait", {})["7"] = {"head": OLD, "base": "stack"}
        self.st.watch_save(watch)
        self.st.human_hold_set(7, fg.HEAD, "person must approve")
        dry = self.unstick([fg.verdict()], dry_run=True)
        real = self.unstick([fg.verdict()], kick=self.gate([]))
        self.assertEqual(dry, real)
        self.assertEqual((real["code"], self.queued, self.kicked), (1, [], []))
        self.assertIn("person must approve", real["lines"][0])
        self.assertEqual(self.keys(), {})

    def test_a_pr_neither_gate_takes_is_owed_no_turn(self):
        # Both gates refuse a draft and a PR by an author the loop does not review.
        for pr, why in (({**fg.LIVE, "draft": True}, "a draft"),
                        ({**fg.LIVE, "user": {"login": "stranger"}}, "by stranger, not a fixer")):
            with self.subTest(why=why):
                for reviews in ([fg.verdict()], []):
                    out = self.unstick(reviews, pr=pr)
                    self.assertEqual((out["code"], self.queued, self.kicked), (1, [], []))
                    self.assertIn(why, out["lines"][0])

    def test_an_unreadable_ledger_takes_no_step(self):
        # Review of #2760, F11: a locked ledger read as empty hid a running turn of another key,
        # and a second turn was queued beside it. The read fails only for unstick's decision; the
        # gate a delivery would reach reads it again, recovered.
        connect, locked = views.read_only_connect, [True]

        def read_only_connect(db, *args, **kwargs):
            if locked[0]:
                raise sqlite3.OperationalError("database is locked")
            return connect(db, *args, **kwargs)

        def recovered(loop, pr, head, sender):
            locked[0] = False
            return deliver(loop, pr, head, sender)
        deliver = self.gate([])
        self.turn("running", head=fg.HEAD, seat="reviewer", key="dismissed:9")
        self.turn("running", head=fg.HEAD, key="cifix:")
        with mock.patch.object(views, "read_only_connect", side_effect=read_only_connect):
            for reviews in ([], [fg.verdict()]):
                for dry_run in (True, False):
                    with self.subTest(reviews=len(reviews), dry_run=dry_run):
                        locked[0] = True
                        out = self.unstick(reviews, dry_run=dry_run, kick=recovered)
                        self.assertEqual((out["code"], self.queued, self.kicked), (2, [], []))
                        self.assertIn("database is locked", out["lines"][0])
        self.assertEqual(self.keys(), {"dismissed:9": "running"})

    def test_a_review_marked_in_flight_is_waited_for_and_an_ended_turn_still_re_armed(self):
        # The reviewer gate refuses a request while the head's review is marked out.
        self.st.inflight(f"review:7:{fg.HEAD}", record=True)
        dry = self.unstick([], dry_run=True)
        real = self.unstick([], kick=self.gate([]))
        self.assertEqual(dry, real)
        self.assertEqual((real["code"], self.kicked), (1, []))
        self.assertIn("marked in flight until", real["lines"][0])
        self.turn("failed", TIMEOUT, head=fg.HEAD, seat="reviewer")
        self.assertIn(f"would {unstick.REREVIEW}", self.unstick([], dry_run=True)["lines"][0])

    def test_a_verdict_that_cannot_be_ordered_takes_no_step(self):
        for state in ("CHANGES_REQUESTED", "APPROVED"):
            with self.subTest(state=state):
                undated = {**fg.verdict(5, state), "submitted_at": None}
                out = self.unstick([undated])
                self.assertEqual((out["code"], self.queued, self.kicked), (2, [], []))
                self.assertIn("latest verdict at this head cannot be told", out["lines"][0])

    def test_a_retarget_without_a_baseline_is_said(self):
        self.st.transition_set(7, {"head": fg.HEAD, "from_base": "stack", "at": 1000,
                                   "old_review_ids": None})
        out = self.unstick([], dry_run=True)
        self.assertEqual(out["code"], 1)
        self.assertIn(transition.MISSING_BASELINE, out["lines"][0])

    def test_a_fix_turn_retry_would_refuse_is_said_dry_and_for_real(self):
        # Review of #2760, F5: the dry run promised a re-arm retry then refused.
        for state, error in (("succeeded", ""), ("cancelled", SUPERSEDED)):
            with self.subTest(state=state):
                ended = self.turn(state, error, head=fg.HEAD)
                dry = self.unstick([fg.verdict()], dry_run=True)
                real = self.unstick([fg.verdict()])
                self.assertEqual(dry, real, "the dry run says what the command does")
                self.assertEqual((real["code"], self.queued), (1, []))
                self.assertIn(f"ended {state}, and retry would not re-arm it", real["lines"][0])
                self.assertEqual(self.state(ended), state)
                with contextlib.closing(sqlite3.connect(self.db)) as con, con:
                    con.execute("DELETE FROM runs")
        self.turn("failed", TIMEOUT, head=fg.HEAD)
        self.assertIn(f"would {unstick.REFIX}",
                      self.unstick([fg.verdict()], dry_run=True)["lines"][0])

    def test_a_head_left_to_a_persons_approval_is_owed_no_review(self):
        # Review of #2760, F3: human_paths, as the sweep and the reviewer gate read it.
        self.st.human_hold_set(7, fg.HEAD, "person must approve")
        out = self.unstick([])
        self.assertEqual((out["code"], self.kicked), (1, []))
        self.assertIn("person must approve", out["lines"][0])
        self.assertEqual(self.unstick([])["code"], 1)

    def test_what_is_not_the_loops_to_do_is_said_and_left(self):
        approved = [fg.verdict(6, "APPROVED")]
        capped = [{**fg.verdict(i), "submitted_at": f"2026-01-01T00:0{i}:00Z"} for i in (1, 2, 3)]
        for reviews, why in ((approved, "approved"), (capped, "3/3 verdicts spent")):
            with self.subTest(why=why):
                out = self.unstick(reviews)
                self.assertEqual((out["code"], self.queued, self.kicked), (1, [], []))
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
