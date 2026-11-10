"""#1004 part 3: a shadow review the adjudicator upheld goes to the fixer.

When the reviewer approved a head, the shadow review there requested changes, and the adjudicator
ruled REJECT on it, the shadow's findings become a fixer turn (``shadow:<ruling time>``), queued by
the ruling's own delivery and, as a backstop, by the watchdog sweep. The turn is claimed and
launched only while that ruling still stands at the head; its prompt carries the shadow review and
the ruling as the work order; its push needs no changes-requested review (the head is approved)
but that same ruling, still current. The pushed head is reviewed fresh.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
import test_shadow_escalate_ruling as ruling  # noqa: E402
from diaktoros import broker, gate, gh, run_supervisor, safe_push  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402


class Upheld(ruling.Base):
    def upheld(self):
        return gate.upheld_shadow_ruling(self.loop, 7, fg.HEAD, db=self.db)

    def test_only_a_reject_answering_the_newest_shadow_block_upholds_it(self):
        self.assertIsNone(self.upheld())                       # nothing recorded
        self.shadow("REQUEST_CHANGES")
        self.assertIsNone(self.upheld())                       # no ruling yet
        self.ruled("ACCEPT")
        self.assertIsNone(self.upheld())                       # overruled
        self.ruled("REJECT")
        self.assertIsInstance(self.upheld(), float)            # upheld: its time
        self.shadow("REQUEST_CHANGES")                         # a newer review it never answered
        self.assertIsNone(self.upheld())
        self.shadow("APPROVE")
        self.ruled("REJECT")
        self.assertIsNone(self.upheld())                       # a shadow approval upholds nothing

    def test_the_hold_says_who_answers_an_upheld_review(self):
        # The shadow on #1342: it must not promise a fix that queue_shadow_fix will not queue.
        self.shadow("REQUEST_CHANGES")
        marker = self.mark_escalated()
        self.ruled("REJECT")

        def held(author="fixer"):
            with mock.patch.object(gh, "pr", return_value={**fg.LIVE, "user": {"login": author}}):
                kind, text, _note = gate.shadow_state(self.loop, 7, fg.HEAD, db=self.db,
                                                      marker=marker)
            self.assertEqual(kind, "failure")                  # never ready to merge
            return text
        self.assertIn("no fixer takes it here, so the decision is yours", held())   # push off
        self.loop = {**self.loop, "unattended_fixer_push": True}
        self.assertIn("the fixer answers its findings", held())
        self.assertIn("the decision is yours", held(author="someone"))             # review-only


class Operator(ruling.Base):
    """#1353: ``ready`` and ``explain`` agree with the shadow hold on who answers an upheld review:
    the fixer on a fixer's PR of a loop that pushes unattended, a person everywhere else."""

    def upheld_marker(self):
        self.shadow("REQUEST_CHANGES")
        marker = self.mark_escalated()          # the marker stays ``adjudicating`` after a ruling
        self.ruled("REJECT")
        return marker

    def ready(self, author="fixer"):
        from diaktoros import ready
        pr = {**fg.LIVE, "user": {"login": author}}
        with mock.patch.object(gh, "pr", return_value=pr), \
                mock.patch.object(ready.gate_failures, "open_for", return_value=[]):
            return ready.assess(self.loop, self.st, pr, ruling.APPROVED, None, None)

    def explain(self, author="fixer"):
        from diaktoros import explain
        pr = {**fg.LIVE, "user": {"login": author}}
        with mock.patch.object(gh, "pr", return_value=pr):
            held = gate.shadow_hold(self.loop, 7, fg.HEAD, db=self.db,
                                    marker=self.st.breach_get(7))
            facts = {"pr": pr, "reviews": ruling.APPROVED, "armed": True, "read_at": 0,
                     "shadow_hold": held}
            return explain.explain(self.loop, self.st, 7, facts)

    def test_ready_says_only_the_fixer_answers_it(self):
        self.upheld_marker()
        self.loop = {**self.loop, "unattended_fixer_push": True}
        why = self.ready()
        self.assertTrue(any("the fixer answers its findings" in w for w in why), why)
        self.assertFalse(any(w.startswith("escalated:") for w in why), why)   # no "a person decides"

    def test_ready_still_leaves_it_to_a_person_where_no_fixer_takes_it(self):
        self.upheld_marker()
        why = self.ready()                                                    # push off
        self.assertTrue(any("a person decides" in w for w in why), why)
        self.loop = {**self.loop, "unattended_fixer_push": True}
        why = self.ready(author="someone")                                    # review-only
        self.assertTrue(any("a person decides" in w for w in why), why)

    def test_explain_hands_it_to_the_fixer_not_to_you(self):
        self.upheld_marker()
        self.loop = {**self.loop, "unattended_fixer_push": True}
        report = self.explain()
        self.assertFalse(any(b.startswith(("ruled:", "escalated:")) for b in report["blockers"]),
                         report["blockers"])
        self.assertTrue(any("the fixer answers its findings" in b for b in report["blockers"]),
                        report["blockers"])
        self.assertEqual(report["next"]["kind"], "fixer-push", report["next"])
        self.assertNotIn("your decision", report["next"]["action"])

    def test_explain_still_leaves_it_to_you_where_no_fixer_takes_it(self):
        self.upheld_marker()
        report = self.explain()                                               # push off
        self.assertTrue(any(b.startswith("ruled:") for b in report["blockers"]), report["blockers"])
        self.assertIn("your decision", report["next"]["action"])


class Queue(ruling.Base):
    def queue(self, author="fixer"):
        live = {**fg.LIVE, "user": {"login": author}}
        with mock.patch.object(gh, "pr", return_value=live), \
                mock.patch.object(gate, "enqueue_isolated", return_value="enqueued") as enqueue:
            outcome = gate.queue_shadow_fix(self.loop, 7, fg.HEAD, db=self.db)
        return outcome, enqueue

    def test_an_upheld_ruling_queues_one_fixer_turn_keyed_by_the_ruling(self):
        self.shadow("REQUEST_CHANGES")
        self.ruled("REJECT")
        ruled = gate.upheld_shadow_ruling(self.loop, 7, fg.HEAD, db=self.db)
        self.loop = {**self.loop, "unattended_fixer_push": True}
        outcome, enqueue = self.queue()
        self.assertEqual(outcome, "enqueued")
        enqueue.assert_called_once_with(self.loop, "fixer", 7, fg.HEAD,
                                        turn_key=f"shadow:{int(ruled)}")

    def test_nothing_is_queued_without_an_upheld_ruling_push_or_fixer(self):
        self.shadow("REQUEST_CHANGES")
        self.assertEqual(self.queue()[0], "")                  # no ruling
        self.ruled("REJECT")
        outcome, enqueue = self.queue()
        self.assertEqual(outcome, "push off")                  # the loop has not opted in
        enqueue.assert_not_called()
        self.loop = {**self.loop, "unattended_fixer_push": True}
        outcome, enqueue = self.queue(author="someone")
        self.assertEqual(outcome, "not a fixer PR")            # a review-only author answers it
        enqueue.assert_not_called()

    # The broker's ruling path, borrowed (not inherited) from the #1112 tests.
    claim = ruling.Between.claim
    rule = ruling.Between.rule

    def test_the_rulings_own_delivery_queues_the_fix_and_only_for_a_reject(self):
        self.shadow("REQUEST_CHANGES")
        for verdict, calls in (("ACCEPT", 0), ("REJECT", 1)):
            with self.subTest(verdict=verdict):
                launch, _row = self.claim()
                with mock.patch.object(gate, "queue_shadow_fix", return_value="enqueued") as q:
                    self.rule(launch.scope, verdict)
                self.assertEqual(q.call_count, calls)
                if calls:
                    self.assertEqual(q.call_args.args[1:], (7, fg.HEAD))
                with closing(sqlite3.connect(self.db)) as con, con:
                    con.execute("DELETE FROM runs WHERE seat='adjudicator'")
                    con.execute("DELETE FROM rulings")

    sweep = ruling.CallSites.sweep

    def test_the_watchdog_sweep_queues_it_as_a_backstop(self):
        calls = []
        with mock.patch.object(gate, "shadow_escalation", return_value=""), \
                mock.patch.object(gate, "queue_shadow_fix",
                                  side_effect=lambda *a, **k: calls.append(a[1:3]) or "enqueued"):
            self.sweep()                                       # first sweep: snapshot only
            self.sweep()
        self.assertIn((7, fg.HEAD), calls)


class Launch(ruling.Base):
    def row(self, key):
        sup = run_supervisor.Supervisor(self.db)
        sup.enqueue(f"{fg.REPO}:7:{fg.HEAD}:fixer:{key}", fg.REPO, 7, fg.HEAD, "fixer", turn_key=key)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state='running', owner='w', launch_intent=? "
                        "WHERE seat='fixer'", (time.time(),))
        row = sup.get(f"{fg.REPO}:7:{fg.HEAD}:fixer:{key}")
        return sup, run_supervisor._Turn(row["id"], "w", 60, row=row, loop=self.loop)

    def launch(self, key):
        from diaktoros import ci, issue_facts
        from diaktoros.run_supervisor import pr_facts
        sup, turn = self.row(key)
        change = mock.Mock(partial="", record="RECORD", diff="", author="fixer", description="")
        with mock.patch.object(gh, "reviews", return_value=list(ruling.APPROVED)), \
                mock.patch.object(pr_facts, "effective_reviews", side_effect=lambda l, r, rv, d: rv), \
                mock.patch.object(pr_facts, "pr_change", return_value=change), \
                mock.patch.object(pr_facts, "pr_record", return_value="RECORD"), \
                mock.patch.object(issue_facts, "section", return_value=""), \
                mock.patch.object(ci, "read", return_value=None), \
                mock.patch.object(ci, "section", return_value=""), \
                mock.patch.object(sup, "_policy_hold", return_value=False):
            return sup._prepare_shadow_fix_launch(turn, {"ref": "fix-7"})

    def test_the_work_order_is_the_shadow_review_and_the_ruling(self):
        self.shadow("REQUEST_CHANGES", body="F1 [test]: a.py:1: the token is logged")
        self.ruled("REJECT")
        ruled = gate.upheld_shadow_ruling(self.loop, 7, fg.HEAD, db=self.db)
        launch = self.launch(f"shadow:{int(ruled)}")
        self.assertIn("Your work order: an upheld second review", launch.prompt)
        self.assertIn("F1 [test]: a.py:1: the token is logged", launch.prompt)
        self.assertIn("because", launch.prompt)                # the ruling's body
        self.assertEqual(launch.scope.shadow_fix, ruled)

    def test_a_ruling_no_longer_current_ends_the_run(self):
        self.shadow("REQUEST_CHANGES")
        self.ruled("REJECT")
        ruled = gate.upheld_shadow_ruling(self.loop, 7, fg.HEAD, db=self.db)
        self.ruled("ACCEPT")                                   # overruled since it was queued
        with self.assertRaisesRegex(ValueError, "upheld shadow ruling no longer current"):
            self.launch(f"shadow:{int(ruled)}")


class Claim(ruling.Base):
    row = Launch.row

    def claim_read(self, key):
        from diaktoros import config
        from diaktoros.run_supervisor import turns
        self.loop = {**self.loop, "unattended_fixer_push": True}
        sup, turn = self.row(key)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET push_admitted=1 WHERE seat='fixer'")
        row = sup.get(f"{fg.REPO}:7:{fg.HEAD}:fixer:{key}")
        read = turns._ClaimRead()
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
                mock.patch.object(gh, "api", return_value=dict(fg.LIVE)):
            sup._claim_read_fixer(row, read)
        return read

    def test_the_production_claim_order_claims_it_past_the_shadow_escalation(self):
        # The shadow on #1342: the escalation marker stays at the head after the ruling, and
        # _claim_read_live cancelled every fixer row at an escalated head before the ruling
        # check was reached. Driven in the order _claim uses, with that marker in place.
        from diaktoros import config, pr_hold
        from diaktoros.run_supervisor import turns
        self.shadow("REQUEST_CHANGES")
        self.mark_escalated()
        self.ruled("REJECT")
        ruled = gate.upheld_shadow_ruling(self.loop, 7, fg.HEAD, db=self.db)
        key = f"shadow:{int(ruled)}"
        self.loop = {**self.loop, "unattended_fixer_push": True}
        sup, _turn = self.row(key)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state='pending', owner=NULL, push_admitted=1 "
                        "WHERE seat='fixer'")
        row = sup.get(f"{fg.REPO}:7:{fg.HEAD}:fixer:{key}")
        read = turns._ClaimRead()
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
                mock.patch.object(gh, "api", return_value=dict(fg.LIVE)), \
                mock.patch.object(pr_hold, "read", return_value=(None, "")):
            sup._claim_read_live(row, read)
            sup._claim_read_fixer(row, read)
        self.assertFalse(read.superseded or read.read_error or read.refused, vars(read))
        # Any other fixer row at the escalated head is still retired, as before.
        other = turns._ClaimRead()
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
                mock.patch.object(pr_hold, "read", return_value=(None, "")):
            sup._claim_read_live({**dict(row), "turn_key": ""}, other)
        self.assertEqual(other.superseded, "escalated to adjudication at this head")

    def test_a_claim_needs_the_ruling_still_standing_not_a_verdict(self):
        self.shadow("REQUEST_CHANGES")
        self.ruled("REJECT")
        ruled = gate.upheld_shadow_ruling(self.loop, 7, fg.HEAD, db=self.db)
        read = self.claim_read(f"shadow:{int(ruled)}")
        self.assertFalse(read.superseded or read.read_error or read.refused)   # claimable
        self.ruled("ACCEPT")                                   # overruled before the claim
        read = self.claim_read(f"shadow:{int(ruled)}")
        self.assertEqual(read.superseded, "upheld shadow ruling no longer current")


class Push(ruling.Base):
    def push(self, shadow_fix):
        manifest = {"base": fg.HEAD, "files": {}, "patch": ""}
        with mock.patch.object(safe_push, "_manifest", return_value=(fg.HEAD, {}, "")), \
                mock.patch.object(broker, "authorize",
                                  side_effect=broker.BrokerDenied("stop here")) as authorize:
            self.loop = {**self.loop, "unattended_fixer_push": True}
            with self.assertRaises(broker.BrokerDenied) as denied:
                safe_push.push(self.loop, repo=fg.REPO, number=7, head=fg.HEAD, role="fixer",
                               branch="fix-7", manifest=manifest, shadow_fix=shadow_fix)
        return str(denied.exception), authorize

    def test_the_push_needs_the_ruling_not_a_verdict(self):
        self.shadow("REQUEST_CHANGES")
        self.ruled("REJECT")
        ruled = gate.upheld_shadow_ruling(self.loop, 7, fg.HEAD, db=self.db)
        with mock.patch.object(run_supervisor, "production_ledger", return_value=self.db):
            reason, authorize = self.push(ruled)
        self.assertEqual(reason, "stop here")                  # past the ruling check
        self.assertIs(authorize.call_args.kwargs["require_verdict"], False)
        with mock.patch.object(run_supervisor, "production_ledger", return_value=self.db):
            reason, authorize = self.push(ruled + 1)           # not the ruling that stands
        self.assertEqual(reason, "upheld shadow ruling no longer current")
        authorize.assert_not_called()

    def test_the_broker_push_reads_the_ruling_from_the_runs_ledger(self):
        # #1354: the broker's push checks the ruling in RunScope.ledger_db, as the claim and the
        # launch do, not in the production ledger (which here holds no ruling at all).
        from diaktoros import broker_ipc
        self.set_push(True)
        self.db = str(self.root / "run-ledger.sqlite")
        run_supervisor.Supervisor(self.db)
        self.shadow("REQUEST_CHANGES")
        self.ruled("REJECT")
        ruled = gate.upheld_shadow_ruling(self.loop, 7, fg.HEAD, db=self.db)
        self.assertIsNotNone(ruled)
        self.assertIsNone(gate.upheld_shadow_ruling(self.loop, 7, fg.HEAD))  # not production's
        scope = broker_ipc.RunScope(fg.REPO, 7, fg.HEAD, "fixer", "fix-7", "rid", self.db,
                                    shadow_fix=ruled)
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        supervisor = run_supervisor.Supervisor
        raw = b'{"operation":"push","manifest":{}}\n'
        with mock.patch.object(safe_push, "_manifest", return_value=(fg.HEAD, {}, "")), \
                mock.patch.object(broker_ipc.RunBroker, "_policy_hold", return_value=""), \
                mock.patch.object(broker_ipc.RunBroker, "_partial_view", return_value=""), \
                mock.patch.object(supervisor, "push_admitted", return_value=True), \
                mock.patch.object(supervisor, "begin_push"), \
                mock.patch.object(supervisor, "abort_push") as aborted, \
                mock.patch.object(broker, "authorize",
                                  side_effect=broker.BrokerDenied("stop here")) as authorize:
            with self.assertRaises(broker.BrokerDenied) as denied:
                server._dispatch(raw)
        self.assertEqual(str(denied.exception), "stop here")   # past the ruling check
        self.assertIs(authorize.call_args.kwargs["require_verdict"], False)
        aborted.assert_called_once()


if __name__ == "__main__":
    unittest.main()
