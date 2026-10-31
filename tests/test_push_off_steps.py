"""With unattended fixer pushes off, ``explain`` tells three states apart (#1506).

1. A queued push-off hold: enabling pushes, then the sweep or a drain, starts the run.
2. A fixer turn that already started (a lock, an in-flight mark, a ledger turn): it cannot
   publish. The worker cancels it before launch, or the broker refuses its push; once pushes are
   on, ``hermes dk retry`` re-arms it. At a spent cap too: it is no "the fixer pushes a fix".
3. No work order at all: nothing starts one by itself, so after enabling the verdict is
   re-delivered to the fixer gate.

A queue entry the gate declined (a terminal turn) is no push-off hold: the drain leaves it, so
it too needs the retry. Each state is run through the sweep or the drain after enabling.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import config, explain, gate, run_supervisor  # noqa: E402

PUSHES_A_FIX = "the fixer pushes a fix"


class Steps(fg.Base):
    def report(self, held=None, inflight_fix=False, queued="", verdicts=1):
        local = {"held": held or {}, "queued_seat": "fixer" if queued else "",
                 "queued_reason": queued, "inflight_review": False, "inflight_fix": inflight_fix,
                 "marker": {}, "parked": False, "delivery_status": "",
                 "capacity": {"reviewer": (0, 1), "fixer": (0, 1)}, "stale_queues": [],
                 "seat": "", "queue": "", "inflight": "", "escalation": "", "sweep": ""}
        self.loop["adjudicator"] = {"route": "one-breach"}
        with mock.patch.object(explain, "_explain_state", return_value=local):
            return explain.explain(self.loop, self.st, 7, {
                "pr": fg.LIVE, "reviews": [fg.verdict(rid) for rid in range(1, verdicts + 1)],
                "armed": True, "read_at": time.time()})

    def retry(self):
        return f"hermes dk retry --loop {self.loop['id']} --pr 7 --seat fixer"


class PushOff(Steps):
    def test_a_started_fixer_turn_cannot_publish_and_retry_re_arms_it(self):
        self.set_push(False)
        held = {"fixer": {"head": fg.HEAD, "at": time.time()}}
        queued = mock.patch.object(run_supervisor.views, "turn_state", side_effect=lambda db, repo, pr,
                                   head, seat: "pending" if seat == "fixer" else None)
        for name, kw, ledger in (("held", {"held": held}, None), ("in flight", {"inflight_fix": True}, None),
                                 ("in the ledger", {}, queued)):
            with self.subTest(name), ledger or contextlib.nullcontext():
                report = self.report(**kw)
                step = report["next"]
                self.assertEqual(step["kind"], "operator")
                self.assertNotIn(PUSHES_A_FIX, step["action"])
                self.assertIn(config.fixer_push_enable_command(self.loop), step["action"])
                self.assertIn(self.retry(), step["action"])
                self.assertNotIn("starts the fix run", step["action"])   # the sweep drains a queue
                self.assertTrue(any("cannot publish" in b for b in report["blockers"]), report)
                self.assertFalse([b for b in report["blockers"] if "starts no fixer turn" in b])

    def test_at_the_spent_cap_a_started_turn_is_no_promised_push(self):
        self.set_push(False)
        report = self.report(inflight_fix=True, verdicts=3)
        self.assertEqual(report["next"]["kind"], "operator")
        self.assertNotIn(PUSHES_A_FIX, report["next"]["action"])
        self.assertIn(self.retry(), report["next"]["action"])
        self.assertIn("with the cap spent, its push then goes for a ruling", report["next"]["action"])
        self.assertTrue(any("cannot publish" in b for b in report["blockers"]), report)
        self.set_push(True)                                   # pushes on: the push is next again
        self.assertEqual(self.report(inflight_fix=True, verdicts=3)["next"]["kind"], "fixer-push")

    def test_a_queued_hold_is_started_by_the_sweep_or_a_drain(self):
        self.set_push(False)
        step = self.report(queued=config.fixer_push_hold_reason(self.loop))["next"]
        self.assertEqual(step["kind"], "operator")
        self.assertIn("the next watchdog sweep (or `hermes dk drain", step["action"])
        self.assertNotIn(self.retry(), step["action"])

    def test_no_work_order_is_re_delivered_after_enabling(self):
        self.set_push(False)
        report = self.report()
        step = report["next"]
        self.assertEqual(step["kind"], "operator")
        self.assertIn("no fixer work order", step["action"])
        self.assertIn("re-deliver the changes-requested review event for head aaaaaaa", step["action"])
        self.assertNotIn("starts the fix run", step["action"])
        self.assertTrue(any("starts no fixer turn" in b for b in report["blockers"]))


class AfterEnabling(Steps):
    """What really happens once pushes are back on, for the states explain names (#1510 review:
    a started turn and a declined one are each run through the sweep or the drain)."""

    production = fg.SupervisorAdmission.production

    def sweep(self, verdicts=1):
        with mock.patch.object(fg.watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(fg.watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(fg.gh, "open_prs", return_value=[fg.LIVE]), \
             mock.patch.object(fg.gh, "reviews",
                               return_value=[fg.verdict(rid) for rid in range(1, verdicts + 1)]), \
             mock.patch.object(fg.watchdog, "retry_pending_breaches"), \
             mock.patch.object(fg.watchdog.routes, "fire") as fire, \
             mock.patch.object(gate, "breach") as breach, \
             mock.patch.object(fg.watchdog.observer, "notify") as notify, \
             mock.patch.object(fg.watchdog.observer, "retry", return_value=0), \
             mock.patch.object(fg.watchdog.observer, "flush"):
            fg.watchdog.sweep_loop(self.loop, self.st)
        return fire, breach, notify

    def started_turn(self, verdicts):
        """An admitted fixer turn, then pushes off: explain names the retry, the claim cancels
        it, and once pushes are on the sweep re-delivers nothing; the retry re-arms it."""
        self.set_push(True)
        sup = self.production()
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("run", fg.REPO, 7, fg.HEAD, "fixer", require_push_admission=True)
        self.set_push(False)
        step = self.report(verdicts=verdicts)["next"]          # pending in the ledger: started
        self.assertEqual(step["kind"], "operator")
        self.assertNotIn(PUSHES_A_FIX, step["action"])
        self.assertIn(self.retry(), step["action"])
        with mock.patch.object(fg.gh, "api"):
            self.assertIsNone(sup._claim())
        self.assertEqual(sup.get("run")["error"], run_supervisor.FIXER_PUSH_REVOKED)
        self.set_push(True)
        fire, breach, notify = self.sweep(verdicts)
        fire.assert_not_called()                               # no re-delivery to the fixer
        breach.assert_not_called()
        notify.assert_not_called()
        self.assertEqual(sup.get("run")["state"], "cancelled")
        with mock.patch.object(sup, "_spawn"):
            sup.retry(sup.get("run")["id"])
        self.assertEqual(sup.get("run")["state"], "pending")

    def test_below_the_cap_a_started_turn_needs_the_retry(self):
        self.started_turn(1)

    def test_at_the_spent_cap_a_started_turn_needs_the_retry_too(self):
        self.started_turn(3)

    def test_the_sweep_starts_no_verdict_without_a_work_order(self):
        self.set_push(True)
        fire, _, _ = self.sweep()                               # no queue entry, no run
        self.assertFalse([c for c in fire.call_args_list if c.args[1] == "pull_request_review"])

    def drain(self):
        def delivered(*_args, **_kw):                          # the gate enqueues and pops it
            self.st.queue_pop("fixer", f"{fg.REPO}#7")
            return True
        with mock.patch.object(fg.watchdog.routes, "fire", side_effect=delivered) as fire, \
             mock.patch.object(fg.gh, "pr", return_value=fg.LIVE), \
             mock.patch.object(fg.gh, "reviews", return_value=[fg.verdict()]):
            return fg.watchdog.drain(self.loop, self.st, "fixer", quiet=True), fire

    def test_a_queued_hold_is_started_by_the_drain(self):
        gate.hold_fixer_push_off(self.loop, self.st, 7, fg.HEAD)
        self.set_push(True)
        started, fire = self.drain()
        self.assertEqual(started, 1)
        self.assertEqual(fire.call_args.args[1], "pull_request_review")

    def test_a_declined_fixer_turn_is_left_by_the_drain_so_explain_names_the_retry(self):
        # #1510 review: a declined entry read as a push-off hold, promising the sweep or a drain.
        declined = gate.declined_reason("fixer", "duplicate failed")
        step = self.report(queued=declined)["next"]
        self.assertEqual(step["kind"], "operator")
        self.assertNotIn("starts the fix run", step["action"])
        self.assertIn(self.retry(), step["action"])
        self.st.queue_add("fixer", f"{fg.REPO}#7", fg.HEAD, "url", declined)
        self.set_push(True)
        started, fire = self.drain()
        self.assertEqual(started, 0)
        fire.assert_not_called()
        self.assertTrue(gate.is_declined_hold(self.st.queue_items("fixer")[f"{fg.REPO}#7"]))


if __name__ == "__main__":
    unittest.main()
