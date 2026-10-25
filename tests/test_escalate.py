"""#459: `hermes dk escalate --pr N` sends one PR to the adjudicator now, whatever its count.

The escalation writes the breach marker a spent cap writes, marked `escalated`, and from then on
that head is parked as if its cap were spent: the ruling turn passes the worker's pre-launch check
below the cap, both gates start nothing more there, a queued review or fix is retired, the
watchdog retries a pending delivery, and `explain` shows it parked. A new head supersedes it.
Offline: GitHub is mocked; config, state and the run ledger are real, in a temporary home.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import cli, config, escalate_pending, gate, gh, ledger, one_shot_action, run_supervisor, state as state_mod  # noqa: E402
from diaktoros.run_supervisor import pr_facts  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402
from scripts import gate_fixer, gate_reviewer  # noqa: E402


HEAD = fg.HEAD
OTHER = "c" * 40


def verdict(rid=5, state="CHANGES_REQUESTED", head=HEAD):
    return {"id": rid, "state": state, "commit_id": head,
            "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "reviewer"}}


class Base(fg.Base):
    def setUp(self):
        super().setUp()
        self.configure(adjudicator=True)
        self.reviews = [verdict()]
        self.live = dict(fg.LIVE)

    def configure(self, *, adjudicator: bool, push: bool = True):
        path = config.config_dir() / "one.json"
        data = {**fg.raw_loop(push), "state_dir": str(self.root / "state")}
        if adjudicator:
            data["adjudicator"] = {"route": "one-breach"}
        path.write_text(json.dumps(data))
        self.loop = config.load_id("one")
        self.st = state_mod.state_for(self.loop)

    @contextlib.contextmanager
    def github(self):
        with mock.patch.object(gh, "pr", side_effect=lambda *a, **k: self.live), \
                mock.patch.object(gh, "reviews", side_effect=lambda *a, **k: self.reviews), \
                mock.patch.object(gate, "wake_adjudicator", return_value=True) as wake, \
                mock.patch.object(gate.observer, "notify"):
            yield wake

    def escalate(self, reason="talking past each other", withdraw=False):
        with self.github() as wake, contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_escalate(argparse.Namespace(loop="one", pr=7, reason=reason,
                                                     withdraw=withdraw))
        return rc, out.getvalue(), wake

    def running(self, seat):
        sup = Supervisor(config.home() / "state" / "diaktoros-runs.sqlite")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d", fg.REPO, 7, HEAD, seat)
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running'")
        return sup

    def fixer_gate(self, review, reviews):
        """Drive the fixer gate on one verdict webhook; (stderr, enqueue mock, breach mock)."""
        payload = {"action": "submitted", "repository": {"full_name": fg.REPO},
                   "pull_request": fg.LIVE, "review": review}
        with mock.patch.object(gate_fixer.sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(gate_fixer.gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gate_fixer.gh, "reviews", return_value=reviews), \
                mock.patch.object(gate_fixer.gate, "fetch_reviews", return_value=reviews), \
                mock.patch.object(gate_fixer.gate, "drain_seat"), \
                mock.patch.object(gate, "enqueue_isolated", return_value="enqueued") as enqueue, \
                mock.patch.object(gate, "breach", return_value="new") as breach, \
                mock.patch.object(gate_fixer.observer, "notify") as notify, \
                mock.patch.object(gate_fixer.observer, "verified_base_sha",
                                  side_effect=lambda loop, pr: (pr.get("base") or {}).get("sha")), \
                mock.patch.object(gate_fixer.observer, "base_tip", return_value=""):
            with self.assertRaises(SystemExit):
                gate_fixer.main()
        return err.getvalue(), enqueue, breach, notify


class Command(Base):
    def test_operator_can_escalate_review_only_pr_and_adjudicator_accepts_it(self):
        self.loop = config.load_id("one")
        path = config.config_dir() / "one.json"
        data = json.loads(path.read_text())
        data["review_only"] = ["owner-human"]
        path.write_text(json.dumps(data))
        self.loop = config.load_id("one")
        self.live = {**fg.LIVE, "user": {"login": "Owner-Human"}}
        rc, out, wake = self.escalate("person asks for ruling")
        self.assertEqual(rc, 0, out)
        wake.assert_called_once()
        marker = self.st.breach_get(7)
        self.assertEqual(marker["status"], "awaiting-adjudication")
        self.assertEqual(marker["escalated"]["by"], "operator")

        from diaktoros.run_supervisor import pr_facts
        pr = {**self.live, "head": {"sha": HEAD, "ref": "review-only-7"}}
        row = {"repo": fg.REPO, "pr": 7, "head": HEAD, "seat": "adjudicator",
               "turn_key": "breach:1", "id": "ruling-1", "created": time.time()}
        with mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=self.reviews), \
                mock.patch.object(pr_facts, "effective_reviews", return_value=self.reviews):
            self.assertEqual(pr_facts.adjudication_state(self.loop, row)[0], "ok")
        from diaktoros.run_supervisor.turn_prompts import adjudication_situation
        self.assertIn("operator asked for a ruling", adjudication_situation(marker))



    def test_a_review_only_pr_with_a_turn_running_is_refused_not_left_pending(self):
        # #1154 + #1246: a pending escalation takes effect in the fixer gate, which returns a
        # review-only verdict to its author first; there it would only lapse. A review-only PR
        # has no fixer turn, so it is free once this turn ends: still the plain refusal.
        path = config.config_dir() / "one.json"
        data = json.loads(path.read_text())
        data["review_only"] = ["owner-human"]
        path.write_text(json.dumps(data))
        self.loop = config.load_id("one")
        self.live = {**fg.LIVE, "user": {"login": "owner-human"}}
        self.running("reviewer")
        rc, out, wake = self.escalate()
        self.assertEqual(rc, 1, out)
        self.assertIn("in flight", out)
        wake.assert_not_called()
        self.assertIsNone(escalate_pending.get(self.st, 7))

    def test_a_maintainer_only_pr_is_refused(self):
        # #1165: escalation needs a verdict, and the reviewer never reviews a maintainer who is
        # not review-only, so the old allowance was unreachable. It is refused instead, and the
        # worker does not accept such an escalation either.
        self.live = {**fg.LIVE, "user": {"login": "Trusted-Maintainer"}}
        with mock.patch.object(config, "maintainers", return_value={"trusted-maintainer"}):
            rc, out, wake = self.escalate("maintainer asks for ruling")
            self.assertEqual(rc, 1, out)
            self.assertIn("only an open, ready PR by a fixer or review-only author", out)
            wake.assert_not_called()
            self.assertEqual(self.st.breach_get(7), {})
            pr = {**self.live, "head": {"sha": HEAD, "ref": "maintainer-7"}}
            row = {"repo": fg.REPO, "pr": 7, "head": HEAD, "seat": "adjudicator",
                   "turn_key": "breach:1", "id": "ruling-maintainer", "created": time.time()}
            self.st.breach.parent.mkdir(parents=True, exist_ok=True)
            state_mod._atomic_write(self.st.breach, {gate.seat_key(self.loop, 7): {
                "pr": 7, "head": HEAD, "rounds": 1, "cap": 3, "reason": "r", "at": "now",
                "status": "awaiting-adjudication", "escalated": {"by": "operator", "reason": "r"}}})
            with (mock.patch.object(gh, "api", return_value=pr),
                  mock.patch.object(gh, "reviews", return_value=self.reviews),
                  mock.patch.object(pr_facts, "effective_reviews", return_value=self.reviews)):
                self.assertEqual(pr_facts.adjudication_state(self.loop, row)[0], "superseded")

    def test_breach_itself_does_not_park_a_maintainer_only_pr(self):
        # #1165, defence in depth: an operator escalation reaching gate.breach for a maintainer
        # who is not review-only is stale (nothing parked), not a ruling on a PR with no verdict.
        self.live = {**fg.LIVE, "user": {"login": "trusted-maintainer"}}
        with self.github() as wake, \
                mock.patch.object(config, "maintainers", return_value={"trusted-maintainer"}):
            outcome = gate.breach(self.loop, self.st, 7, HEAD, 1, "escalated: r",
                                  escalated={"by": "operator", "reason": "r"})
        self.assertEqual(outcome, "stale")
        wake.assert_not_called()
        self.assertEqual(self.st.breach_get(7), {})

    def test_an_escalation_parks_the_head_and_wakes_the_adjudicator(self):
        rc, out, wake = self.escalate("they disagree\non scope")
        self.assertEqual(rc, 0, out)
        self.assertIn("escalated at 1/3 verdicts", out)
        wake.assert_called_once()
        marker = self.st.breach_get(7)
        self.assertEqual((marker["head"], marker["rounds"], marker["status"]),
                         (HEAD, 1, "awaiting-adjudication"))
        self.assertEqual(marker["escalated"]["by"], "operator")
        self.assertNotIn("\n", marker["escalated"]["reason"])
        self.assertTrue(gate.escalated(marker, HEAD))
        self.assertFalse(gate.escalated(marker, OTHER))          # a new head supersedes it
        rc, out, _ = self.escalate()
        self.assertEqual(rc, 1)
        self.assertIn("already awaiting a ruling", out)
        with ledger.connect(run_supervisor.production_ledger()) as con:   # the report's (#678)
            self.assertEqual(con.execute("SELECT pr, action FROM operator_actions").fetchall(),
                             [(7, "escalate")])

    def test_refusals(self):
        cases = {
            "no adjudicator": lambda: self.configure(adjudicator=False),
            "approved at this head": lambda: setattr(self, "reviews", [verdict(state="APPROVED")]),
            "no verdict yet": lambda: setattr(self, "reviews", []),
            "only an open, ready PR": lambda: setattr(self, "live", {**fg.LIVE, "draft": True}),
        }
        for want, arrange in cases.items():
            with self.subTest(want=want):
                self.configure(adjudicator=True)
                self.reviews, self.live = [verdict()], dict(fg.LIVE)
                arrange()
                rc, out, wake = self.escalate()
                self.assertEqual(rc, 1, out)
                self.assertIn(want, out)
                wake.assert_not_called()
                self.assertEqual(self.st.breach_get(7), {})

    def test_a_run_in_flight_records_a_pending_escalation(self):
        # #1246: no longer a refusal: with fixer pushes on there is never a gap between runs.
        self.running("reviewer")
        rc, out, wake = self.escalate()
        self.assertEqual(rc, 0, out)
        self.assertIn("escalation pending", out)
        wake.assert_not_called()
        self.assertEqual(self.st.breach_get(7), {})               # nothing parked yet
        self.assertEqual(escalate_pending.get(self.st, 7)["reason"], "talking past each other")

    def test_a_running_shadow_review_does_not_refuse(self):
        # #218: a shadow writes nothing at the head, so it holds no escalation.
        sup = Supervisor(config.home() / "state" / "diaktoros-runs.sqlite")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d", fg.REPO, 7, HEAD, "shadow")
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running'")
        rc, out, wake = self.escalate()
        self.assertEqual(rc, 0, out)
        wake.assert_called_once()


class Parked(Base):
    def setUp(self):
        super().setUp()
        self.assertEqual(self.escalate()[0], 0)

    def set_marker(self, marker: dict) -> None:
        # breach_set never re-arms a claimed head, by design; a test that needs another marker
        # state writes the loop's breach file as the state module does.
        state_mod._atomic_write(self.st.breach, {f"{fg.REPO}#7": marker})

    def row(self, seat="adjudicator"):
        return {"repo": fg.REPO, "pr": 7, "head": HEAD, "seat": seat, "turn_key": "breach:1",
                "id": "r1", "created": time.time()}

    def adjudication(self):
        pr = {**fg.LIVE, "head": {"sha": HEAD, "ref": "fix-7"}}
        with mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=self.reviews), \
                mock.patch.object(pr_facts, "effective_reviews",
                                  side_effect=lambda loop, row, reviews, db=None: reviews):
            return pr_facts.adjudication_state(self.loop, self.row())[0]

    def test_the_ruling_runs_below_the_cap_only_when_escalated(self):
        self.assertEqual(self.adjudication(), "ok")
        marker = self.st.breach_get(7)
        self.set_marker({k: v for k, v in marker.items() if k != "escalated"})
        self.assertEqual(self.adjudication(), "superseded")

    def test_the_fixer_gate_starts_no_fix_at_an_escalated_head(self):
        payload = {"action": "submitted", "repository": {"full_name": fg.REPO},
                   "pull_request": fg.LIVE, "review": verdict(6) | {"state": "changes_requested"}}
        with mock.patch.object(gate_fixer.sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(gate_fixer.gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gate_fixer.gate, "fetch_reviews", return_value=[verdict(5), verdict(6)]), \
                mock.patch.object(gate_fixer.gate, "drain_seat"), \
                mock.patch.object(gate, "enqueue_isolated") as enqueue, \
                mock.patch.object(gate, "breach") as breach, \
                mock.patch.object(gate_fixer.observer, "notify"):
            with self.assertRaises(SystemExit):
                gate_fixer.main()
        enqueue.assert_not_called()
        breach.assert_not_called()
        self.assertIn("escalated to adjudication", err.getvalue())

    def test_the_reviewer_gate_starts_no_review_at_an_escalated_head(self):
        payload = {"action": "review_requested", "number": 7, "pull_request": fg.LIVE,
                   "repository": {"full_name": fg.REPO}, "sender": {"login": "fixer"},
                   "requested_reviewer": {"login": "reviewer"}}
        self.reviews = [verdict(head=OTHER)]                    # earlier round, older head
        with mock.patch.object(gate_reviewer.sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(gate_reviewer.gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gate_reviewer.gate, "fetch_reviews", return_value=self.reviews), \
                mock.patch.object(gate_reviewer.gate, "block_pr_agent") as block, \
                mock.patch.object(gate_reviewer.observer, "notify"):
            with self.assertRaises(SystemExit):
                gate_reviewer.main()
        block.assert_not_called()
        self.assertIn("escalated to adjudication", err.getvalue())

    def test_a_queued_fix_at_the_escalated_head_is_retired(self):
        settings = self.root / "runtime.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        sup = Supervisor(config.home() / "state" / "diaktoros-runs.sqlite",
                         production_config=settings, hermes_home=self.root / "home")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("f", fg.REPO, 7, HEAD, "fixer", require_push_admission=True)
        pr = {**fg.LIVE, "head": {"sha": HEAD, "ref": "fix-7", "repo": {"full_name": fg.REPO}}}
        with mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=self.reviews):
            self.assertIsNone(sup._claim())
        row = sup.get("f")
        self.assertNotEqual(row["state"], "claimed")
        self.assertIn("escalated to adjudication", str(row["error"] or ""))

    def explain_parked(self, ruled=None):
        local = {"held": {}, "queued_seat": "", "queued_reason": "", "inflight_review": False,
                 "inflight_fix": False, "marker": self.st.breach_get(7), "parked": True,
                 "delivery_status": "adjudicating",
                 "capacity": {"reviewer": (0, 1), "fixer": (0, 1)}, "stale_queues": [],
                 "seat": "", "queue": "", "inflight": "", "escalation": "", "sweep": ""}
        with mock.patch.object(gate, "_explain_state", return_value=local), \
                mock.patch.object(gate, "_ruled", return_value=ruled):
            return gate.explain(self.loop, self.st, 7, {"pr": fg.LIVE, "reviews": self.reviews,
                                                        "armed": True, "read_at": time.time()})

    def test_explain_after_the_ruling_says_the_decision_is_yours(self):
        # Live on #537: explain still said the adjudicator rules next after its ruling posted.
        report = self.explain_parked(ruled="REJECT")
        self.assertEqual(report["next"]["kind"], "operator")
        self.assertIn("the adjudicator ruled REJECT", report["next"]["action"])
        self.assertTrue(any("ruled: the adjudicator ruled REJECT" in b for b in report["blockers"]))
        self.assertFalse(any("parked awaiting adjudication" in b for b in report["blockers"]))
        self.assertEqual(self.explain_parked()["next"]["kind"], "adjudication")

    def test_explain_shows_it_parked(self):
        local = {"held": {}, "queued_seat": "", "queued_reason": "", "inflight_review": False,
                 "inflight_fix": False, "marker": self.st.breach_get(7), "parked": True,
                 "delivery_status": "awaiting-adjudication",
                 "capacity": {"reviewer": (0, 1), "fixer": (0, 1)}, "stale_queues": [],
                 "seat": "", "queue": "", "inflight": "", "escalation": "", "sweep": ""}
        with mock.patch.object(gate, "_explain_state", return_value=local):
            report = gate.explain(self.loop, self.st, 7, {"pr": fg.LIVE, "reviews": self.reviews,
                                                          "armed": True, "read_at": time.time()})
        self.assertEqual(report["next"]["kind"], "adjudication")

    def test_the_watchdog_retries_a_pending_escalation(self):
        from scripts import watchdog
        marker = self.st.breach_get(7)
        self.set_marker({**marker, "status": "delivery-pending"})
        with mock.patch.object(gh, "reviews", return_value=self.reviews), \
                mock.patch.object(gate, "breach") as breach:
            watchdog.retry_pending_breaches(self.loop, self.st, [fg.LIVE])
        breach.assert_called_once()
        self.assertEqual(breach.call_args.kwargs["escalated"]["by"], "operator")


class Pending(Base):
    """#1246: an escalation asked for while a run is in flight takes effect at the next
    changes-requested verdict; an approval drops it."""

    def setUp(self):
        super().setUp()
        escalate_pending.record(self.st, 7, reason="stuck on scope", head=HEAD)

    def test_the_next_changes_requested_verdict_goes_to_the_adjudicator(self):
        err, enqueue, breach, _ = self.fixer_gate(
            verdict(6) | {"state": "changes_requested"}, [verdict(5), verdict(6)])
        enqueue.assert_not_called()                                  # no fixer turn
        breach.assert_called_once()
        args, kwargs = breach.call_args
        self.assertEqual(args[3:6], (HEAD, 2, "escalated: stuck on scope"))
        self.assertEqual(kwargs["escalated"]["reason"], "stuck on scope")
        self.assertIn("pending escalation taken", err)
        self.assertIsNone(escalate_pending.get(self.st, 7))

    def test_the_cap_comes_first_and_clears_it(self):
        reviews = [verdict(4), verdict(5), verdict(6)]               # 3 of 3: the cap breaches
        err, enqueue, breach, _ = self.fixer_gate(verdict(6) | {"state": "changes_requested"},
                                                  reviews)
        breach.assert_called_once()
        self.assertIn("loop exhausted", breach.call_args.args[5])
        self.assertIsNone(escalate_pending.get(self.st, 7))

    def test_an_approval_of_the_current_head_drops_it_with_a_notice(self):
        approved = verdict(6, state="APPROVED")
        err, enqueue, breach, notify = self.fixer_gate(approved | {"state": "approved"},
                                                       [verdict(5), approved])
        breach.assert_not_called()
        self.assertIsNone(escalate_pending.get(self.st, 7))
        events = [c.args[2] for c in notify.call_args_list]
        self.assertIn("escalation", events)
        [dropped] = [c for c in notify.call_args_list if c.args[2] == "escalation"]
        self.assertIn("pending escalation dropped", dropped.kwargs["outcome"])

    def test_an_approval_of_an_older_head_keeps_it(self):
        old = verdict(6, state="APPROVED", head=OTHER)
        self.fixer_gate(old | {"state": "approved"}, [verdict(5), old])
        self.assertIsNotNone(escalate_pending.get(self.st, 7))

    def test_withdraw_lapse_and_prune(self):
        rc, out, _ = self.escalate(withdraw=True)
        self.assertEqual(rc, 0)
        self.assertIn("withdrawn (stuck on scope)", out)
        self.assertIsNone(escalate_pending.get(self.st, 7))
        self.assertIn("no pending escalation", self.escalate(withdraw=True)[1])
        entry = escalate_pending.record(self.st, 7, reason="r", head=HEAD)
        with mock.patch.object(escalate_pending.time, "time",
                               return_value=entry["at"] + escalate_pending.TTL_S + 1):
            self.assertIsNone(escalate_pending.get(self.st, 7))      # lapsed after 24h
        escalate_pending.record(self.st, 8, reason="r", head=HEAD)
        self.assertEqual(escalate_pending.prune(self.st, [7]), [8])  # 8 is no longer open
        self.assertIsNotNone(escalate_pending.get(self.st, 7))

    def test_reading_it_writes_nothing(self):
        # explain is read-only (the harness hashes every file): get() and a no-op take() never
        # create a lock or touch the store.
        before = sorted(p.name for p in self.st.dir.iterdir())
        with mock.patch.object(self.st, "locked", side_effect=AssertionError("took the lock")):
            self.assertIsNotNone(escalate_pending.get(self.st, 7))
            self.assertIsNone(escalate_pending.take(self.st, 99))
        self.assertEqual(sorted(p.name for p in self.st.dir.iterdir()), before)
        self.assertIsNone(escalate_pending.take(mock.Mock(), 7))   # unreadable: none pending

    def test_desktop_still_refuses_and_names_the_cli(self):
        escalate_pending.take(self.st, 7)
        self.running("fixer")
        with self.github():
            offer = one_shot_action.plan(self.loop, "escalate", pr=7)
        self.assertFalse(offer["eligible"])
        self.assertIn("host CLI can queue", offer["reason"])
        self.assertIsNone(escalate_pending.get(self.st, 7))

    def test_a_normal_escalation_supersedes_it_and_explain_shows_it(self):
        state = gate._explain_state(self.loop, self.st, gate.seat_key(self.loop, 7), 7, HEAD,
                                    time.time())
        self.assertIn("pending since", state["escalation"])
        self.assertIn("stuck on scope", state["escalation"])
        rc, out, _ = self.escalate()                                  # nothing in flight now
        self.assertEqual(rc, 0, out)
        self.assertIsNone(escalate_pending.get(self.st, 7))


if __name__ == "__main__":
    unittest.main()
