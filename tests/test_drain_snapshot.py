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
        """The route POST: the gate takes the queue entry (it queued the run) when ``consume``."""
        def fire(route, event, payload, tag, host, on_attempt=None, on_unsent=None):
            self.fired.append(tag)
            if on_attempt:
                on_attempt()
            if consume:
                number = (payload.get("pull_request") or {}).get("number")
                self.st().queue_pop("reviewer", f"{t.REPO}#{number}")
            return True
        return mock.patch.object(routes, "fire", side_effect=fire)

    def drain(self, expected):
        return actions.drain_confirmed(self.loop(), "reviewer", expected)

    def test_the_confirmed_queue_is_drained_and_each_run_reported(self):
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        self.assertEqual([row[:2] for row in shown], [[f"{t.REPO}#7", t.HEAD_A]])
        with self.answer():
            result = self.drain(shown)
        self.assertEqual((result["ok"], result["status"], result["started"]), (True, "started", 1))
        self.assertEqual([(o["number"], o["outcome"]) for o in result["outcomes"]], [(7, "started")])
        self.assertEqual(self.fired, ["drain-review-7"])

    def test_a_queue_that_changed_since_the_confirmation_starts_nothing(self):
        t.reset(prs={"7": t.pr(7), "8": t.pr(8)})
        self.queue(7)
        shown = actions.drain_snapshot(self.loop(), "reviewer")
        self.queue(8)                                   # a new request since the preview
        before = self.st().queue_items("reviewer")
        with self.answer():
            result = self.drain(shown)
        self.assertEqual((result["ok"], result["status"], result["started"]), (False, "stale", 0))
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

    def test_an_unanswered_post_is_unknown(self):
        t.reset(prs={"7": t.pr(7)})
        self.queue(7)
        with self.answer(consume=False):
            result = self.drain(actions.drain_snapshot(self.loop(), "reviewer"))
        self.assertEqual((result["ok"], result["status"]), (False, "unknown"))
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
        self.assertEqual((result["status"], result["started"]), ("started", 1))
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
        self.assertEqual((result["ok"], result["status"], result["started"]), (False, "unknown", 1))
        self.assertEqual(sorted((o["number"], o["outcome"]) for o in result["outcomes"]),
                         [(7, "started"), (8, "left")])

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
