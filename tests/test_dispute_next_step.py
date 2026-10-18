"""A fixer's dispute is an answer with a next step (#894, #895, finishing #401).

The fixer answered every finding as not a defect and pushed nothing (#879, #883). The sweep called
that a silent "fixer never pushed" stall, and nothing woke anyone: the reviewer won't re-review an
unchanged head (by design, #401), and the cap wasn't spent. With an adjudicator configured, the
sweep now sends the head for a ruling, as `hermes dk escalate` would; without one, the alert says
what happened and what the operator can do.
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_fixer_gating as fg  # noqa: E402
import test_stall_live_turn as sl  # noqa: E402
from diaktoros import gate, run_supervisor  # noqa: E402

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
        self.assertIn("hermes dk escalate --pr 7", text)

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
