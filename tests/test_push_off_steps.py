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
    def report(self, held=None, inflight_fix=False, queued="", verdicts=1, seat="fixer"):
        local = {"held": held or {}, "queued_seat": seat if queued else "",
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
    def test_a_started_fixer_turn_cannot_publish_and_names_what_re_arms_it(self):
        self.set_push(False)
        held = {"fixer": {"head": fg.HEAD, "at": time.time()}}
        queued = mock.patch.object(run_supervisor.views, "turn_state", side_effect=lambda db, repo, pr,
                                   head, seat: "pending" if seat == "fixer" else None)
        redeliver = "re-deliver the changes-requested review event for head aaaaaaa to the fixer gate"
        # #1510 review F4: a lock or a mark with no run in the ledger has nothing for retry to
        # re-arm; a fresh delivery is the recovery there, the retry only for a ledgered run.
        for name, kw, ledger, recovery in (("held", {"held": held}, None, redeliver),
                                           ("in flight", {"inflight_fix": True}, None, redeliver),
                                           ("in the ledger", {}, queued, self.retry())):
            with self.subTest(name), ledger or contextlib.nullcontext():
                report = self.report(**kw)
                step = report["next"]
                self.assertEqual(step["kind"], "operator")
                self.assertNotIn(PUSHES_A_FIX, step["action"])
                self.assertIn(config.fixer_push_enable_command(self.loop), step["action"])
                self.assertIn(recovery, step["action"])
                self.assertNotIn(self.retry() if recovery == redeliver else redeliver, step["action"])
                self.assertNotIn("starts the fix run", step["action"])   # the sweep drains a queue
                self.assertTrue(any("cannot publish" in b for b in report["blockers"]), report)
                self.assertFalse([b for b in report["blockers"] if "starts no fixer turn" in b])

    def test_at_the_spent_cap_a_started_turn_is_no_promised_push(self):
        self.set_push(False)
        with mock.patch.object(run_supervisor.views, "turn_state", side_effect=lambda db, repo, pr,
                               head, seat: "running" if seat == "fixer" else None):
            report = self.report(inflight_fix=True, verdicts=3)
        self.assertEqual(report["next"]["kind"], "operator")
        self.assertNotIn(PUSHES_A_FIX, report["next"]["action"])
        self.assertIn(self.retry(), report["next"]["action"])
        self.assertIn("with the cap spent, its push then goes for a ruling", report["next"]["action"])
        self.assertTrue(any("cannot publish" in b for b in report["blockers"]), report)
        # No run behind the mark: nothing to retry, and the cap verdict is no work order (#1494),
        # so escalate is the step once pushes are on, never a re-delivery (#1510 review F4).
        bare = self.report(inflight_fix=True, verdicts=3)["next"]["action"]
        self.assertNotIn(self.retry(), bare)
        self.assertNotIn("re-deliver", bare)
        self.assertIn(f"hermes dk escalate --loop {self.loop['id']} --pr 7", bare)
        self.set_push(True)                                   # pushes on: the push is next again
        self.assertEqual(self.report(inflight_fix=True, verdicts=3)["next"]["kind"], "fixer-push")

    def test_a_queued_hold_is_started_by_the_sweep_or_a_drain(self):
        self.set_push(False)
        step = self.report(queued=config.fixer_push_hold_reason(self.loop))["next"]
        self.assertEqual(step["kind"], "operator")
        self.assertIn("the next watchdog sweep (or `hermes dk drain", step["action"])
        self.assertNotIn(self.retry(), step["action"])

    def test_a_capacity_queued_fixer_entry_is_no_push_off_hold(self):
        # Shadow on #1510: every fixer queue entry got the hold's "the next sweep or drain starts
        # it"; only a FIXER_PUSH_HOLD entry does. A capacity entry waits for a slot as well.
        self.set_push(False)
        step = self.report(queued="waiting for a slot")["next"]
        self.assertEqual(step["kind"], "operator")
        self.assertNotIn("then starts the fix run", step["action"])
        self.assertIn("when a fixer slot frees", step["action"])
        self.assertIn("waiting for a slot", step["action"])
        self.assertIn(config.fixer_push_enable_command(self.loop), step["action"])

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

    def test_a_lock_or_mark_with_no_run_is_recovered_by_a_fresh_delivery_not_retry(self):
        # #1510 review F4, after enabling: retry has nothing, the sweep delivers nothing, and the
        # re-delivered verdict is what the fixer gate admits.
        from diaktoros import retry_action
        sup = self.production()                                # a ledger with no run at all
        for kw in ({"held": {"fixer": {"head": fg.HEAD, "at": time.time()}}}, {"inflight_fix": True}):
            self.set_push(False)
            step = self.report(**kw)["next"]
            self.assertNotIn(self.retry(), step["action"])
            self.assertIn("re-deliver the changes-requested review event", step["action"])
        self.set_push(True)
        with sup._connect() as con:
            self.assertIn("no isolated run on record", retry_action.plan(con, fg.REPO, 7, "fixer")["message"])
        fire, _, _ = self.sweep()
        fire.assert_not_called()
        enqueue, _ = fg.GateHold.run_gate(self)                 # the re-delivered verdict starts it
        enqueue.assert_called_once()

    def test_a_ledgered_turn_that_is_not_live_is_never_sent_a_re_delivery(self):
        # Shadow on #1510: an uncertain fixer turn fell to "no run", naming a re-delivery the
        # ledger refuses as a duplicate; retry refuses a run that may have written (reconcile).
        for state, expect in (("uncertain", "reconcile it first"), ("failed", self.retry()),
                              ("succeeded", "nothing to retry — fixer succeeded")):
            with self.subTest(state):
                sup, _ = self.ledgered(state)
                self.set_push(False)
                report = self.report()
                step = report["next"]
                self.assertIn(expect, step["action"])
                self.assertNotIn("re-deliver", step["action"])
                # Shadow on 2ef4ac8: the blocker beside it said the verdict "starts no fixer turn".
                self.assertFalse([b for b in report["blockers"] if "starts no fixer turn" in b])
                self.assertTrue(any(f"is {state} with no fix pushed" in b for b in report["blockers"]))
                self.assertNotIn("no run", step["action"])
                if state != "failed":
                    self.assertNotIn(self.retry(), step["action"])
                self.set_push(True)
                with mock.patch.object(sup, "_spawn"):
                    outcome = sup.submit("again", fg.REPO, 7, fg.HEAD, "fixer")
                self.assertEqual(outcome.startswith(f"duplicate {state}"), state != "failed", outcome)
                with sup._connect() as con:
                    con.execute("DELETE FROM runs")

    def test_a_lock_or_mark_over_an_ended_run_follows_the_ledger_not_the_mark(self):
        # #1510 review F5: a mark or lock can outlive its run (state.py keeps them until cleared
        # or expired); the started-turn step promised retry for a row retry has nothing for.
        held = {"fixer": {"head": fg.HEAD, "at": time.time()}}
        for state, kw, expect in (("succeeded", {"inflight_fix": True}, "nothing to retry — fixer succeeded"),
                                  ("uncertain", {"held": held}, "reconcile it first"),
                                  ("failed", {"inflight_fix": True}, self.retry())):
            for verdicts in (1, 3):
                with self.subTest(state=state, verdicts=verdicts):
                    sup, _ = self.ledgered(state)
                    self.set_push(False)
                    action = self.report(verdicts=verdicts, **kw)["next"]["action"]
                    self.assertIn(expect, action)
                    self.assertNotIn(PUSHES_A_FIX, action)
                    self.assertNotIn("re-deliver", action)
                    if state != "failed":
                        self.assertNotIn(self.retry(), action)
                    with sup._connect() as con:
                        con.execute("DELETE FROM runs")

    def test_a_live_in_flight_mark_holds_the_re_delivery_until_it_expires(self):
        # #1510 review F6/F7: the fixer gate silences a re-delivery while the head's fix mark
        # stands, so the step names the wait; with the mark gone the same delivery is enqueued.
        self.production()                                      # a ledger with no run at all
        mark = f"fix:7:{fg.HEAD}"
        self.st.inflight(mark, record=True)
        self.set_push(False)
        action = self.report(inflight_fix=True)["next"]["action"]
        self.assertIn("refuses a re-delivery while the in-flight fix mark", action)
        self.assertIn(f"ttl {self.st._minutes('inflight_ttl_min'):g}m", action)
        self.assertIn("once it has expired, re-deliver the changes-requested review event", action)
        self.assertNotIn(self.retry(), action)
        self.set_push(True)
        enqueue, _ = fg.GateHold.run_gate(self)
        enqueue.assert_not_called()                            # the mark holds the gate
        self.st.inflight_clear(mark)
        enqueue, _ = fg.GateHold.run_gate(self)
        enqueue.assert_called_once()
        # A bare lock holds nothing: the plain re-delivery, no wait named.
        self.set_push(False)
        bare = self.report(held={"fixer": {"head": fg.HEAD, "at": time.time()}})["next"]["action"]
        self.assertNotIn("in-flight fix mark", bare)
        self.assertIn("re-deliver the changes-requested review event", bare)

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

    def ledgered(self, state):
        """A fixer turn at this head in ``state``, and the queue entry the gate declined it with."""
        sup = self.production()
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("run", fg.REPO, 7, fg.HEAD, "fixer")
        with sup._connect() as con:
            con.execute("UPDATE runs SET state=?", (state,))
        return sup, gate.declined_reason("fixer", f"duplicate {state}")

    def test_a_declined_fixer_turn_is_left_by_the_drain_so_explain_names_the_retry(self):
        # #1510 review: a declined entry read as a push-off hold, promising the sweep or a drain.
        sup, declined = self.ledgered("failed")
        report = self.report(queued=declined)
        step = report["next"]
        self.assertFalse([b for b in report["blockers"] if "starts no fixer turn" in b])
        # The failed row behind the declined entry is the blocker here; a declined entry with no
        # row at all gets its own (the ended turn is classified first, in step and blocker alike).
        self.assertTrue(any("is failed with no fix pushed" in b for b in report["blockers"]), report["blockers"])
        with sup._connect() as con:
            con.execute("DELETE FROM runs")
        bare = self.report(queued=declined)["blockers"]
        self.assertTrue(any("declined the fixer turn" in b for b in bare), bare)
        self.assertFalse([b for b in bare if "starts no fixer turn" in b])
        self.assertEqual(step["kind"], "operator")
        self.assertNotIn("starts the fix run", step["action"])
        self.assertIn(self.retry(), step["action"])
        self.st.queue_add("fixer", f"{fg.REPO}#7", fg.HEAD, "url", declined)
        self.set_push(True)
        started, fire = self.drain()
        self.assertEqual(started, 0)
        fire.assert_not_called()
        self.assertTrue(gate.is_declined_hold(self.st.queue_items("fixer")[f"{fg.REPO}#7"]))

    def test_a_declined_turn_retry_would_refuse_is_not_offered_the_retry(self):
        # #1510 review F3: a declined `duplicate succeeded` was told to retry, which says
        # "nothing to retry"; explain now asks the same plan the retry runs.
        from diaktoros import retry_action
        for state, why in (("succeeded", "nothing to retry — fixer succeeded"),
                           ("uncertain", "may have written")):
            with self.subTest(state):
                sup, declined = self.ledgered(state)
                step = self.report(queued=declined)["next"]
                self.assertNotIn(self.retry(), step["action"])
                self.assertIn(why, step["action"])
                self.assertIn("fix it by hand, push, and re-request review", step["action"])
                with sup._connect() as con:
                    self.assertEqual(retry_action.plan(con, fg.REPO, 7, "fixer")["rearmable"], [])
                with sup._connect() as con:
                    con.execute("DELETE FROM runs")


    def test_a_reviewer_queue_entry_is_no_fixer_work_order(self):
        # Shadow review on #1510: the last queue entry read is any seat's, so a declined or
        # waiting reviewer entry was named a declined fixer turn, or a hold the drain starts.
        self.set_push(False)
        for reason in (gate.declined_reason("reviewer", "duplicate completed"), "waiting for a slot"):
            with self.subTest(reason):
                step = self.report(queued=reason, seat="reviewer")["next"]
                self.assertIn("no fixer work order", step["action"])
                self.assertNotIn("declined the fixer turn", step["action"])
                self.assertNotIn("starts the fix run", step["action"])


if __name__ == "__main__":
    unittest.main()
