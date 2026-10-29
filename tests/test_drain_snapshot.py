"""#1047: a drain bound to a confirmed queue snapshot, reporting what it did per run.

The real watchdog drain runs against the harness's fake GitHub; the route POST is the one stand-in
(``routes.fire``), so a test decides whether the gate answered it.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402

from diaktoros import config, gh, one_shot_action as actions, routes, util  # noqa: E402
from diaktoros import state as state_mod  # noqa: E402


class Drain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        self.fired = []
        hooks = mock.patch("diaktoros.gate.hooks_read", return_value=(True, ""))
        hooks.start()
        self.addCleanup(hooks.stop)

    def loop(self):
        return config.load_id("widgets")

    def st(self):
        return state_mod.state_for(self.loop())

    def queue(self, number, head=None):
        self.st().queue_add("reviewer", f"{t.REPO}#{number}", head or t.HEAD_A,
                            f"https://github.com/{t.REPO}/pull/{number}", "seat busy")

    def answer(self, consume=True):
        """The route POST: when ``consume``, the gate queues the run, as a real gate does: a run
        ledger row for the PR, head and seat, then the queue entry taken."""
        def fire(route, event, payload, tag, host, on_attempt=None, on_unsent=None):
            self.fired.append(tag)
            if on_attempt:
                on_attempt()
            if consume:
                pr = payload.get("pull_request") or {}
                seat = "fixer" if tag.startswith("drain-fix") else "reviewer"
                self.gate_queues(pr.get("number"), (pr.get("head") or {}).get("sha"), seat)
                self.st().queue_pop(seat, f"{t.REPO}#{pr.get('number')}")
            return True
        return mock.patch.object(routes, "fire", side_effect=fire)

    def gate_queues(self, number, head, seat):
        import uuid
        from diaktoros import run_supervisor
        sup = run_supervisor.Supervisor(run_supervisor.production_ledger())
        sup.enqueue(uuid.uuid4().hex, t.REPO, number, head, seat)

    def drain(self, expected):
        return actions.drain_confirmed(self.loop(), "reviewer", expected)

    def test_the_confirmed_queue_is_drained_and_each_run_reported(self):
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        self.assertEqual([row[:2] for row in shown], [[f"{t.REPO}#7", t.HEAD_A]])
        with self.answer():
            result = self.drain(shown)
        self.assertEqual((result["ok"], result["status"], result["sent"]), (False, "sent", 1))
        self.assertEqual([(o["number"], o["outcome"]) for o in result["outcomes"]], [(7, "sent")])
        self.assertEqual(self.fired, ["drain-review-7"])

    def test_a_queue_that_changed_since_the_confirmation_starts_nothing(self):
        t.reset(prs={"7": t.pr(7), "8": t.pr(8)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        self.queue(8)                                   # a new request since the preview
        before = self.st().queue_items("reviewer")
        with self.answer():
            result = self.drain(shown)
        self.assertEqual((result["ok"], result["status"], result["sent"]), (False, "stale", 0))
        self.assertEqual(self.fired, [], "no worker was woken")
        self.assertEqual(self.st().queue_items("reviewer"), before, "nothing dropped or pruned")

    def test_nothing_queued_and_nothing_left_to_start_are_told_apart(self):
        t.reset(prs={"7": t.pr(7, state="closed")})
        self.assertEqual(self.drain([])["status"], "nothing queued")
        self.queue(7)
        result = self.drain(actions.drain_snapshot(self.loop(), "reviewer"))
        self.assertEqual((result["ok"], result["status"]), (True, "nothing to start"))
        self.assertEqual([(o["outcome"], o["why"]) for o in result["outcomes"]],
                         [("dropped", "the PR is closed")])

    def test_a_request_left_queued_is_a_partial_outcome_not_success(self):
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        with mock.patch.object(state_mod.LoopState, "pr_holds", return_value={7: {}}):
            result = self.drain(actions.drain_snapshot(self.loop(), "reviewer"))
        self.assertEqual((result["ok"], result["status"]), (False, "partial"))
        self.assertEqual(result["outcomes"][0]["why"], "a maintainer holds this PR")
        self.assertEqual(self.fired, [])

    def test_a_full_seat_leaves_every_queued_run_and_says_why(self):
        t.reset(prs={"7": t.pr(7), "8": t.pr(8)})
        self.queue(7)
        self.queue(8)
        with mock.patch("diaktoros.config.seat_concurrency", return_value=0), self.answer():
            result = self.drain(actions.drain_snapshot(self.loop(), "reviewer"))
        self.assertEqual((result["ok"], result["status"]), (False, "partial"))
        self.assertEqual([(o["number"], o["outcome"]) for o in result["outcomes"]],
                         [(7, "left"), (8, "left")])
        self.assertTrue(all("at capacity" in o["why"] for o in result["outcomes"]))
        self.assertEqual(self.fired, [])

    def test_the_snapshot_is_canonical_whatever_order_runs_were_queued(self):
        t.reset(prs={"7": t.pr(7), "8": t.pr(8)})
        self.queue(8)
        self.queue(7)
        self.assertEqual([row[0] for row in actions.drain_snapshot(self.loop(), "reviewer")],
                         [f"{t.REPO}#7", f"{t.REPO}#8"])

    def test_a_2xx_the_gate_left_queued_is_sent_and_marked_unverified(self):
        # #1407 round 7 F8: every 2xx is "sent" (#1047's amended "Done when"). An entry the gate
        # left is marked an unverified POST, as the sweep marks it: never left claimed, and never
        # restored as the entry a slower reader could send again.
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        before = self.st().queue_items("reviewer")[f"{t.REPO}#7"]
        with self.answer(consume=False):
            result = self.drain(actions.drain_snapshot(self.loop(), "reviewer"))
        # The 2xx counts as sent, and the entry left behind is reported too (F9).
        self.assertEqual((result["ok"], result["status"], result["sent"]), (False, "partial", 1))
        self.assertEqual([o["outcome"] for o in result["outcomes"]], ["sent", "left"])
        self.assertIn("manual reconciliation required", result["outcomes"][1]["why"])
        after = self.st().queue_items("reviewer")[f"{t.REPO}#7"]
        self.assertNotIn("drain", after)
        self.assertNotEqual(after, before)
        self.assertTrue(after["reason"].startswith("route delivery uncertain"))

    def test_an_unanswered_post_is_unknown(self):
        # No 2xx after the POST was attempted: it may have been sent.
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)

        def fire(route, event, payload, tag, host, on_attempt=None, on_unsent=None):
            self.fired.append(tag)
            on_attempt and on_attempt()
            return False
        with mock.patch.object(routes, "fire", side_effect=fire):
            result = self.drain(actions.drain_snapshot(self.loop(), "reviewer"))
        self.assertEqual((result["ok"], result["status"], result["sent"]), (False, "unknown", 0))
        self.assertEqual(result["outcomes"][0]["outcome"], "uncertain")

    def test_paused_hooks_refuse_before_anything(self):
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        with mock.patch("diaktoros.gate.hooks_read", return_value=(False, "")), self.answer():
            result = self.drain(actions.drain_snapshot(self.loop(), "reviewer"))
        self.assertEqual(result["status"], "refused")
        self.assertEqual(self.fired, [])

    def test_an_unknown_seat_or_a_migration_refuses(self):
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        with self.answer():
            self.assertEqual(actions.drain_confirmed(self.loop(), "triage", [])["status"], "refused")
            with mock.patch("diaktoros.migrate.migrating", return_value={"from": "x"}):
                result = self.drain(actions.drain_snapshot(self.loop(), "reviewer"))
        self.assertEqual(result["status"], "refused")
        self.assertIn("migration", result["outcomes"][0]["why"])
        self.assertEqual(self.fired, [])

    def test_a_run_queued_after_the_check_is_never_woken(self):
        # #1407 review F1: an entry added between the snapshot check and the selection.
        t.reset(prs={"7": t.pr(7), "8": t.pr(8)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        real = state_mod.queue_snapshot

        def then_queue_8(st, seat):
            out = real(st, seat)
            self.queue(8)
            return out
        with mock.patch("diaktoros.config.seat_concurrency", return_value=2), \
                mock.patch.object(state_mod, "queue_snapshot", side_effect=then_queue_8), \
                self.answer():
            result = self.drain(shown)
        self.assertEqual(self.fired, ["drain-review-7"])
        self.assertEqual((result["status"], result["sent"]), ("sent", 1))
        self.assertIn(f"{t.REPO}#8", self.st().queue_items("reviewer"), "left for its own turn")

    def test_an_entry_that_changes_during_the_checks_is_not_woken(self):
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        real_pr = gh.pr

        def pr_then_requeue(loop, number):
            self.queue(7)                       # a newer request for #7 replaces the entry
            return real_pr(loop, number)
        with mock.patch.object(gh, "pr", side_effect=pr_then_requeue), self.answer():
            result = self.drain(shown)
        self.assertEqual(self.fired, [])
        self.assertEqual(result["outcomes"][0]["why"], "the entry changed since it was confirmed")

    def test_an_entry_replaced_after_the_last_check_is_never_sent(self):
        # #1407 round 2 F1: a concurrent drain marks it uncertain after the checks, before the
        # POST. The claim (a compare-and-swap) fails, so nothing is sent twice.
        from diaktoros import drain as drain_mod
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        real = drain_mod._drain_check

        def check_then_replace(loop, st, seat, key, entry, bound=False):
            out = real(loop, st, seat, key, entry, bound=bound)
            st.queue_replace_if(seat, key, entry, entry["head"], entry["url"],
                                "route delivery uncertain — manual reconciliation required")
            return out
        with mock.patch.object(drain_mod, "_drain_check", side_effect=check_then_replace), \
                self.answer():
            result = self.drain(shown)
        self.assertEqual(self.fired, [])
        self.assertEqual((result["status"], result["sent"]), ("partial", 0))
        self.assertEqual(result["outcomes"][0]["why"], "the entry changed since it was confirmed")

    def test_a_concurrent_drain_cannot_mark_an_entry_this_drain_claimed(self):
        # #1407 round 2 F1, as the reviewer's probe interleaves it: another drain read the entry
        # before this one claimed it, and marks it uncertain just before this POST. Its write is
        # a compare-and-swap against what it read, so it fails: one send, no double mark.
        from diaktoros import drain as drain_mod
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        st, key = self.st(), f"{t.REPO}#7"
        seen_before_claim = st.queue_items("reviewer")[key]
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        real = drain_mod._wake
        marked = []

        def other_drain_marks_then_wake(loop, st, seat, key, entry, *a, **k):
            marked.append(st.queue_replace_if(
                seat, key, seen_before_claim, seen_before_claim["head"],
                seen_before_claim["url"], "route delivery uncertain — manual reconciliation required"))
            return real(loop, st, seat, key, entry, *a, **k)
        with mock.patch.object(drain_mod, "_wake", side_effect=other_drain_marks_then_wake), \
                self.answer():
            result = self.drain(shown)
        self.assertEqual(marked, [False], "the other drain's mark lost to this drain's claim")
        self.assertEqual(self.fired, ["drain-review-7"])
        self.assertEqual((result["status"], result["sent"]), ("sent", 1))

    def test_a_sweep_past_its_checks_cannot_send_what_a_confirmed_drain_sent(self):
        # #1407 round 3 F1: the sweep finishes its checks, the confirmed drain claims and sends,
        # then the sweep resumes. Its own claim fails, so there is one POST, not two.
        from diaktoros import drain as drain_mod
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        real, results = drain_mod._drain_check, {}

        def sweep_check_then_confirmed_drain(loop, st, seat, key, entry, bound=False):
            out = real(loop, st, seat, key, entry, bound=bound)
            if not bound and "confirmed" not in results:
                results["confirmed"] = self.drain(shown)
            return out
        with mock.patch.object(drain_mod, "_drain_check", side_effect=sweep_check_then_confirmed_drain), \
                self.answer(consume=False):
            swept = actions._script("watchdog").drain(self.loop(), self.st(), "reviewer", quiet=True)
        self.assertEqual(self.fired, ["drain-review-7"], "one POST")
        self.assertEqual(swept, 0)
        self.assertEqual(results["confirmed"]["outcomes"][0]["outcome"], "sent")

    def test_a_new_request_during_the_post_is_sent_and_kept(self):
        # #1407 round 3 F1: a webhook replaces the claimed entry while the POST is out.
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")

        def fire(route, event, payload, tag, host, on_attempt=None, on_unsent=None):
            self.fired.append(tag)
            on_attempt and on_attempt()
            self.queue(7)                                   # the gate's queue_add of a newer event
            return True
        with mock.patch.object(routes, "fire", side_effect=fire):
            result = self.drain(shown)
        self.assertEqual((result["ok"], result["status"], result["sent"]), (False, "sent", 1))
        self.assertEqual(result["outcomes"][0]["outcome"], "sent")
        self.assertNotIn("drain", self.st().queue_items("reviewer")[f"{t.REPO}#7"],
                         "the newer request stays, unclaimed, for its own turn")

    def test_an_entry_replaced_then_removed_during_the_post_is_not_a_start(self):
        # #1407 round 4 F1: a 2xx, and the entry is gone, but this POST's gate queued nothing
        # (a newer request replaced the claim, then another path removed it).
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")

        def fire(route, event, payload, tag, host, on_attempt=None, on_unsent=None):
            self.fired.append(tag)
            on_attempt and on_attempt()
            self.queue(7)                                           # replaced...
            self.st().queue_pop("reviewer", f"{t.REPO}#7")          # ...then removed
            return True
        with mock.patch.object(routes, "fire", side_effect=fire):
            result = self.drain(shown)
        # Answered, and nothing claims the run started: whether one did is the ledger's.
        self.assertEqual(result["outcomes"][0]["outcome"], "sent")
        self.assertNotIn("started", [o["outcome"] for o in result["outcomes"]])

    def test_a_confirmed_drain_never_claims_a_start_it_cannot_attribute(self):
        # #1407 round 5 F1: a concurrent direct event replaces the claim, queues its own
        # same-head run, and removes its replacement; this POST queued nothing. The ledger
        # cannot tell the runs apart, so a confirmed drain reports "sent", never "started".
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")

        def fire(route, event, payload, tag, host, on_attempt=None, on_unsent=None):
            self.fired.append(tag)
            on_attempt and on_attempt()
            self.queue(7)
            self.gate_queues(7, t.HEAD_A, "reviewer")       # an unrelated same-head run
            self.st().queue_pop("reviewer", f"{t.REPO}#7")
            return True
        with mock.patch.object(routes, "fire", side_effect=fire):
            result = self.drain(shown)
        self.assertNotIn("started", [o["outcome"] for o in result["outcomes"]])
        self.assertNotIn("started", result)
        self.assertEqual(result["outcomes"][0]["outcome"], "sent")
        self.assertIn("whether the gate queued a run is the run ledger's", result["outcomes"][0]["why"])
        self.assertEqual((result["ok"], result["status"]), (False, "sent"))   # never a success

    def test_a_same_head_request_queued_during_the_hold_check_is_kept(self):
        # #1407 round 4 F6: the removal compared only the head, so a newer same-head request
        # was deleted and reported as the confirmed one dropped.
        from diaktoros import transition
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        real_hold = transition.hold

        def hold_then_requeue(st, number, head):
            out = real_hold(st, number, head)
            self.queue(7)
            return {"missing_baseline": True} if out is None else out
        with mock.patch.object(transition, "hold", side_effect=hold_then_requeue), \
                mock.patch.object(transition, "baseline_missing", return_value=True), self.answer():
            result = self.drain(shown)
        self.assertEqual([(o["outcome"], o["why"]) for o in result["outcomes"]],
                         [("left", "the entry changed during the checks")])
        self.assertIn(f"{t.REPO}#7", self.st().queue_items("reviewer"), "the newer request is kept")
        self.assertEqual(self.fired, [])

    def test_a_removal_that_loses_its_compare_is_reported_changed_not_dropped(self):
        # #1407 round 3 F6: #7 is requeued while its closed PR is being read.
        t.reset(prs={"7": t.pr(7, state="closed")})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        real_pr = gh.pr

        def pr_then_requeue(loop, number):
            out = real_pr(loop, number)
            self.queue(7)
            return out
        with mock.patch.object(gh, "pr", side_effect=pr_then_requeue), self.answer():
            result = self.drain(shown)
        self.assertEqual((result["ok"], result["status"]), (False, "partial"))
        self.assertEqual([(o["outcome"], o["why"]) for o in result["outcomes"]],
                         [("left", "the entry changed during the checks")])
        self.assertIn(f"{t.REPO}#7", self.st().queue_items("reviewer"))

    def test_a_confirmed_entry_gone_before_selection_is_reported_not_an_empty_queue(self):
        # #1407 round 2 F5: removed right after the snapshot check.
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        real = state_mod.queue_snapshot

        def check_then_remove(st, seat):
            out = real(st, seat)
            st.queue_pop(seat, f"{t.REPO}#7")
            return out
        with mock.patch.object(state_mod, "queue_snapshot", side_effect=check_then_remove), \
                self.answer():
            result = self.drain(shown)
        self.assertEqual((result["ok"], result["status"]), (False, "partial"))
        self.assertEqual([(o["number"], o["outcome"], o["why"]) for o in result["outcomes"]],
                         [(7, "left", "the entry changed or left the queue since it was confirmed")])

    def test_an_unsent_post_gives_the_claim_back(self):
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        before = self.st().queue_items("reviewer")[f"{t.REPO}#7"]
        with mock.patch.object(routes, "fire", return_value=False):
            result = self.drain(actions.drain_snapshot(self.loop(), "reviewer"))
        self.assertEqual(result["outcomes"][0]["why"], "the route POST was not sent")
        self.assertEqual(self.st().queue_items("reviewer")[f"{t.REPO}#7"], before)

    def test_a_claimed_entry_is_left_alone_and_a_stale_claim_is_an_unverified_post(self):
        from diaktoros import drain as drain_mod
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        st, key = self.st(), f"{t.REPO}#7"
        entry = st.queue_items("reviewer")[key]
        st.queue_swap_if("reviewer", key, entry, dict(entry, drain={"at": 10**12, "id": "x"}))
        watchdog = actions._script("watchdog")
        with self.answer():
            self.assertEqual(watchdog.drain(self.loop(), st, "reviewer", quiet=True), 0)
        self.assertEqual(self.fired, [])
        self.assertIn("drain", st.queue_items("reviewer")[key], "a live claim is left alone")
        claimed = st.queue_items("reviewer")[key]
        stale = dict(claimed, drain={"at": 1.0, "id": "x"})
        st.queue_swap_if("reviewer", key, claimed, stale)
        with self.answer():
            watchdog.drain(self.loop(), st, "reviewer", quiet=True)
        self.assertEqual(self.fired, [])
        self.assertTrue(st.queue_items("reviewer")[key]["reason"].startswith("route delivery uncertain"))
        self.assertGreater(drain_mod.DRAIN_CLAIM_S, 0)

    def test_no_or_a_malformed_snapshot_is_never_a_confirmation(self):
        # #1407 review F2: None disables the check for the sweep; the confirmed door refuses it.
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        with self.answer():
            for bad in (None, "x", [["k", "h"]], [["k", "h", "1"]], [[1, "h", 1.0]], [["k", "h", True]]):
                with self.subTest(expected=bad):
                    result = actions.drain_confirmed(self.loop(), "reviewer", bad)
                    self.assertEqual(result["status"], "refused")
                    self.assertEqual(result["outcomes"][0]["why"], "no confirmed queue snapshot")
        self.assertEqual(self.fired, [])

    def test_a_budget_spent_after_a_start_reports_the_start_and_the_rest(self):
        # #1407 review F3: #7 started, then the budget ran out reading #8.
        t.reset(prs={"7": t.pr(7), "8": t.pr(8)})
        self.queue(7)
        self.queue(8)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        real_pr = gh.pr

        def spent_on_8(loop, number):
            if number == 8:
                raise gh.GateBudgetExceeded("budget")
            return real_pr(loop, number)
        with mock.patch("diaktoros.config.seat_concurrency", return_value=2), \
                mock.patch.object(gh, "pr", side_effect=spent_on_8), self.answer():
            result = self.drain(shown)
        self.assertEqual((result["ok"], result["status"], result["sent"]), (False, "unknown", 1))
        self.assertEqual(sorted((o["number"], o["outcome"]) for o in result["outcomes"]),
                         [(7, "sent"), (8, "left")])

    def test_a_confirmed_drain_never_reconciles_a_retarget(self):
        # #1407 review F4: the sweep's stacked-retarget branch starts a fresh review and
        # rewrites both queues; a confirmed fixer (or reviewer) drain leaves it alone.
        t.reset(prs={"7": t.pr(7)})
        st = self.st()
        st.queue_add("fixer", f"{t.REPO}#7", t.HEAD_A, f"https://github.com/{t.REPO}/pull/7", "busy")
        watch = st.watch()
        watch["stacked_wait"] = {"7": {"base": "feature", "head": t.HEAD_A}}
        st.watch_save(watch)
        with mock.patch("diaktoros.transition.record") as record, \
                mock.patch("diaktoros.transition.start_fresh_review") as fresh, self.answer():
            result = actions.drain_confirmed(self.loop(), "fixer",
                                             actions.drain_snapshot(self.loop(), "fixer"))
        record.assert_not_called()
        fresh.assert_not_called()
        self.assertEqual(result["outcomes"][0]["outcome"], "left")
        self.assertIn("the watchdog's sweep reconciles it", result["outcomes"][0]["why"])
        self.assertIn(f"{t.REPO}#7", self.st().queue_items("fixer"))

    def test_the_drain_changes_nothing_process_wide(self):
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        budget, recorder = dict(gh._GATE), util._DECISION_RECORDER
        with self.answer():
            self.drain(actions.drain_snapshot(self.loop(), "reviewer"))
        self.assertEqual(gh._GATE, budget)
        self.assertIs(util._DECISION_RECORDER, recorder)

    def test_the_desktop_still_refuses_drain_until_its_control_is_wired(self):
        offer = actions.plan(self.loop(), "drain", seat="reviewer")
        self.assertEqual((offer["eligible"], offer["confirmation"]), (False, ""))
        self.assertIn("not wired yet", offer["reason"])

    def test_the_cron_sweep_drains_without_a_snapshot_as_before(self):
        # expected=None is the sweep's call: no snapshot check, and no report needed.
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        watchdog = actions._script("watchdog")
        with self.answer():
            started = watchdog.drain(self.loop(), self.st(), "reviewer", quiet=True)
        self.assertEqual(started, 1)
        self.assertEqual(self.fired, ["drain-review-7"])


if __name__ == "__main__":
    unittest.main()
