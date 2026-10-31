"""A fixer's dispute is an answer with a next step (#894, #895, finishing #401).

The fixer answered every finding as not a defect and pushed nothing (#879, #883). The sweep called
that a silent "fixer never pushed" stall, and nothing woke anyone: the reviewer won't re-review an
unchanged head (by design, #401), and the cap wasn't spent. With an adjudicator configured, the
sweep now sends the head for a ruling, as `hermes dk escalate` would; without one, the alert says
what happened and what the operator can do.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_fixer_gating as fg  # noqa: E402
import test_stall_live_turn as sl  # noqa: E402
from diaktoros import explain, gate, gh, run_supervisor  # noqa: E402

LONG_AGO = "2020-01-01T00:00:00Z"
VERDICT = [{"id": 1, "state": "CHANGES_REQUESTED", "commit_id": fg.HEAD,
            "submitted_at": LONG_AGO, "user": {"login": "reviewer", "id": 2}}]


class Dispute(fg.Base):
    """The sweep, with a dispute in the run ledger. Borrows the live-turn test's sweep."""

    def setUp(self):
        super().setUp()
        self.set_push(True)

    sweep = sl.LiveTurnIsNotAStall.sweep
    arm_long_ago = sl.LiveTurnIsNotAStall.arm_long_ago

    def dispute(self, head=fg.HEAD, comment="posted", created=None):
        sup = run_supervisor.Supervisor(run_supervisor.production_ledger())
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue(f"d{head}{comment}", fg.REPO, 7, head, "fixer")
        run = sup.get(f"d{head}{comment}")["id"]
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET state='succeeded' WHERE id=?", (run,))
            con.execute("INSERT INTO disputes(run_id,repo,pr,head,body,created,comment) "
                        "VALUES(?,?,?,?,?,?,?)", (run, fg.REPO, 7, head, "F1: not a defect",
                                                  time.time() if created is None else created,
                                                  comment))

    def test_without_an_adjudicator_it_is_named_a_dispute_not_a_stall(self):
        self.arm_long_ago()
        self.dispute()
        text = self.sweep(VERDICT)
        self.assertNotIn("fixer never pushed", text)
        self.assertIn("disputed", text)
        self.assertIn("waiting for you", text)
        self.assertIn(f"hermes dk escalate --loop {self.loop['id']} --pr 7", text)   # B4: names its loop

    def test_with_an_adjudicator_it_goes_for_a_ruling_once(self):
        self.arm_long_ago()
        self.loop["adjudicator"] = {"route": "one-breach"}
        self.dispute()
        with mock.patch.object(gate, "breach", return_value="new") as breach:
            text = self.sweep(VERDICT)
        self.assertNotIn("fixer never pushed", text)
        self.assertNotIn("disputed", text)                  # handed off: not an alert
        breach.assert_called_once()
        self.assertEqual(breach.call_args.kwargs["escalated"]["by"], "dispute")
        self.assertEqual(breach.call_args.args[3], fg.HEAD)

    def test_a_dispute_older_than_the_verdict_or_elsewhere_or_unposted_is_no_answer(self):
        cases = {"older than the verdict": dict(created=1.0),
                 "at another head": dict(head="b" * 40),
                 "comment never posted": dict(comment="posting")}
        for name, kwargs in cases.items():
            with self.subTest(name=name):
                self.setUp()
                self.arm_long_ago()
                self.dispute(**kwargs)
                self.assertIn("fixer never pushed", self.sweep(VERDICT))


class Explain(Dispute):
    """B4: ``explain`` on a disputed head says what the sweep says, not "the fixer gate did not
    start one; re-deliver" — a re-delivered verdict asks the fixer again what it already answered."""

    explain = fg.Surfaces.explain

    def test_a_disputed_head_is_a_ruling_or_the_operators_call_never_a_replay(self):
        self.assertEqual(self.explain()["next"]["kind"], "fixer-retry")      # no dispute yet
        self.dispute()
        for route, kind, says in ((None, "operator", f"hermes dk escalate --loop {self.loop['id']} "
                                                     "--pr 7"),
                                  ("one-breach", "adjudication", "watchdog sweep")):
            with self.subTest(route=route):
                self.loop["adjudicator"] = {"route": route} if route else {}
                report = self.explain()
                self.assertEqual(report["next"]["kind"], kind)
                self.assertIn("disputed", report["next"]["action"])
                self.assertIn(says, report["next"]["action"])
                self.assertNotIn("re-deliver", report["next"]["action"])
                self.assertFalse([b for b in report["blockers"] if "did not start one" in b])


class UncertainTurn(Dispute):
    """#1481 review: the sweep counts an uncertain fixer turn as not live, so a dispute behind it
    goes for a ruling; explain must read it the same way, never as a replay."""

    explain = fg.Surfaces.explain

    def test_an_uncertain_fixer_turn_hides_no_dispute_on_either_path(self):
        self.arm_long_ago()
        self.loop["adjudicator"] = {"route": "one-breach"}
        self.dispute()
        uncertain = mock.patch.object(run_supervisor.views, "turn_state",
                                      side_effect=lambda db, repo, pr, head, seat:
                                      "uncertain" if seat == "fixer" else None)
        with uncertain:
            report = self.explain()
        self.assertEqual(report["next"]["kind"], "adjudication")
        self.assertNotIn("re-deliver", report["next"]["action"])
        with uncertain, mock.patch.object(gate, "breach", return_value="new") as breach:
            self.sweep(VERDICT)
        breach.assert_called_once()


class CapStep(fg.Base):
    """A spent cap with no marker (#575): ``hermes dk escalate`` sends the head now, unless a fix
    run is live, when it would only wait for the next verdict (#1481 review): the push is next."""

    def explain(self, inflight_fix, held=None):
        local = {"held": held or {}, "queued_seat": "", "queued_reason": "", "inflight_review": False,
                 "inflight_fix": inflight_fix, "marker": {}, "parked": False,
                 "delivery_status": "", "capacity": {"reviewer": (0, 1), "fixer": (0, 1)},
                 "stale_queues": [], "seat": "", "queue": "", "inflight": "", "escalation": "",
                 "sweep": ""}
        self.loop["adjudicator"] = {"route": "one-breach"}
        with mock.patch.object(explain, "_explain_state", return_value=local):
            return explain.explain(self.loop, self.st, 7, {
                "pr": fg.LIVE, "reviews": [fg.verdict(rid) for rid in (1, 2, 3)], "armed": True,
                "read_at": time.time()})["next"]

    def test_escalate_is_the_step_unless_a_fix_run_is_live(self):
        self.set_push(True)
        idle, fixing = self.explain(False), self.explain(True)
        self.assertEqual(idle["kind"], "operator")
        self.assertIn(f"hermes dk escalate --loop {self.loop['id']} --pr 7", idle["action"])
        self.assertEqual(fixing["kind"], "fixer-push")
        self.assertNotIn("escalate", fixing["action"])
        self.assertIn("reviewer gate then sends it for a ruling", fixing["action"])
        # The operator sees why escalate, not the sweep (#1481 review: it was only a comment).
        self.assertIn("only at a head it saw arrive", idle["action"])

    def test_a_run_on_an_old_head_holds_escalate_as_it_does_explain(self):
        # #1481 review: escalate counts every run of the PR, on any head; with one in flight it
        # only records the request for the next verdict. explain must not promise a ruling then.
        from diaktoros import one_shot_action
        self.set_push(True)
        old = "b" * 40
        sup = run_supervisor.Supervisor(run_supervisor.production_ledger())
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("old-fix", fg.REPO, 7, old, "fixer")
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET state='running' WHERE delivery='old-fix'")
        step = self.explain(False, held={"fixer": {"head": old, "at": time.time()}})
        self.assertEqual(step["kind"], "release")
        self.assertTrue(step["action"].startswith("1 run(s) for this PR are in flight; once they end"))
        with mock.patch.object(gh, "reviews", return_value=[fg.verdict(rid) for rid in (1, 2, 3)]):
            decision = one_shot_action._escalation_decision(self.loop, 7, live=fg.LIVE)
        self.assertIn("1 run(s) for this PR are in flight", decision["reason"])

    def test_a_run_of_unknown_outcome_or_an_unreadable_ledger_holds_it_too(self):
        # #1481 review: a quarantined run is in flight nowhere, yet escalate refuses until it is
        # reconciled; a ledger escalate cannot read leaves it unknown, and explain says so.
        from diaktoros import one_shot_action
        self.set_push(True)
        sup = run_supervisor.Supervisor(run_supervisor.production_ledger())
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("lost", fg.REPO, 7, "b" * 40, "fixer")
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET state='quarantined' WHERE delivery='lost'")
        self.assertEqual(one_shot_action.escalation_blockers(self.loop, 7), (0, True))
        step = self.explain(False)
        self.assertEqual(step["kind"], "release")
        self.assertTrue(step["action"].startswith("a run's outcome is unknown; once it is reconciled"))
        broken = pathlib.Path(sup.db).with_name("no-runs.sqlite")
        with closing(sqlite3.connect(broken)) as con, con:
            con.execute("CREATE TABLE other(x)")
        with mock.patch.object(run_supervisor, "production_ledger", return_value=broken):
            step = self.explain(False)
        self.assertEqual(step["kind"], "release")
        self.assertIn("the run ledger cannot be read", step["action"])

    def test_a_fixer_lock_on_an_old_head_is_no_live_fix(self):
        # #1481 review: an old-head run cannot finish this head, so escalate is still the step.
        stale = self.explain(False, held={"fixer": {"head": "b" * 40, "at": time.time()}})
        self.assertEqual(stale["kind"], "operator")
        self.assertIn("hermes dk escalate", stale["action"])
        current = self.explain(False, held={"fixer": {"head": fg.HEAD, "at": time.time()}})
        self.assertEqual(current["kind"], "fixer-push")


class MaintainerRerequest(Dispute):
    """#894: after a dispute, a maintainer's re-request gets one review at the reviewed head."""

    def gate(self, sender="boss", reviews=VERDICT):
        import contextlib
        import io
        import json
        from scripts import gate_reviewer
        payload = {"action": "review_requested", "number": 7, "pull_request": fg.LIVE,
                   "repository": {"full_name": fg.REPO}, "sender": {"login": sender},
                   "requested_reviewer": {"login": "reviewer"}}
        with mock.patch.object(gate_reviewer.sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                mock.patch.object(gate_reviewer.config, "maintainers", return_value={"boss"}), \
                mock.patch.object(gate_reviewer.gh, "pr", return_value=fg.LIVE), \
                mock.patch.object(gate_reviewer.gate, "fetch_reviews", return_value=list(reviews)), \
                mock.patch.object(gate_reviewer.gate, "block_pr_agent") as block, \
                mock.patch.object(gate_reviewer.observer, "notify"):
            try:
                gate_reviewer.main()
            except SystemExit:
                pass                      # a refusal exits; an accepted request returns
        return block, err.getvalue()

    def test_a_maintainers_rerequest_after_a_dispute_gets_one_review_keyed_by_it(self):
        self.dispute()
        block, _ = self.gate()
        block.assert_called_once()
        self.assertTrue(block.call_args.kwargs["turn_key"].startswith(
            run_supervisor.DISPUTE_KEY))

    def test_no_dispute_or_not_a_maintainer_is_still_refused(self):
        block, err = self.gate()                                   # no dispute
        block.assert_not_called()
        self.assertIn("already has a reviewer's verdict", err)
        self.dispute(created=1.0)                                  # older than the verdict
        block, err = self.gate()
        block.assert_not_called()
        self.assertIn("already has a reviewer's verdict", err)
        self.setUp()
        self.dispute()
        block, err = self.gate(sender="fixer")                     # not a maintainer
        block.assert_not_called()

    def test_a_dispute_escalation_still_takes_it_but_an_operator_escalation_does_not(self):
        self.dispute()
        self.loop["adjudicator"] = {"route": "one-breach"}
        with mock.patch.object(gate, "wake_adjudicator", return_value=True), \
                mock.patch.object(gate.gh, "pr", return_value=fg.LIVE):
            outcome = gate.breach(self.loop, self.st, 7, fg.HEAD, 1, "disputed",
                                  escalated={"by": "dispute", "at": "now", "reason": "r"})
        self.assertEqual(outcome, "new")
        self.assertTrue(gate.escalated(self.st.breach_get(7), fg.HEAD))
        block, _ = self.gate()
        block.assert_called_once()
        marker = self.st.breach_get(7)
        marker["escalated"]["by"] = "operator"
        from diaktoros import state as state_mod
        state_mod._atomic_write(self.st.breach, {f"{fg.REPO}#7": marker})
        block, err = self.gate()
        block.assert_not_called()
        self.assertIn("escalated to adjudication", err)

    def test_the_claim_does_not_cancel_the_rereview(self):
        settings = self.root / "runtime.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        sup = run_supervisor.Supervisor(run_supervisor.production_ledger(),
                                        production_config=settings, hermes_home=self.root / "home")
        from diaktoros import gh
        pr = {**fg.LIVE, "head": {"sha": fg.HEAD, "ref": "fix-7"}}

        def claim(delivery, turn_key):
            with mock.patch.object(sup, "_spawn"):
                sup.enqueue(delivery, fg.REPO, 7, fg.HEAD, "reviewer", turn_key=turn_key)
            with mock.patch.object(gh, "api", return_value=pr), \
                    mock.patch.object(gh, "reviews", return_value=list(VERDICT)), \
                    mock.patch("diaktoros.review_receipt.generation_for", return_value="g"):
                sup._claim()
            return sup.get(delivery)
        # A plain reviewer row at a reviewed head is retired, as before ...
        self.assertIn("reviewed at this head", str(claim("plain", "")["error"] or ""))
        # ... the dispute re-review is claimed.
        self.assertEqual(claim("rr", "dispute:1")["state"], "claimed")


if __name__ == "__main__":
    unittest.main()
