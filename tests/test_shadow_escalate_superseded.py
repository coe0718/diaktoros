"""#2601, #2623, #2688: a shadow escalation's ruling whose shadow review is missing, not unreadable.

The review a ruling answers is the active reviewer turn's (#1683). A newer reviewer turn at the
same head (``review --reconsider`` queues one) leaves the ledger readable but with no review for
that turn yet. The launch waits for it within the retry backoff, saying what it waits on rather
than "shadow review unreadable", so the turn's own review, landing minutes later, is still ruled
on with nobody involved (#2688); one that agrees is not ruled on, as the gate would not escalate
it, and its escalation is cleared once that run has failed, so a later dissent escalates anew and
re-arms it (#2688 review F1). An unreadable ledger still waits as before (#1145).
Driven through the real launch (``_prepare_launch``) on a real run ledger.
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
import test_shadow_escalate_ruling as ser  # noqa: E402
from diaktoros import run_supervisor  # noqa: E402
from diaktoros.run_supervisor import turn_prompts, views  # noqa: E402

ROW = {"repo": fg.REPO, "pr": 7, "head": fg.HEAD}


class Superseded(ser.Base):
    claim = ser.Between.claim          # the real launch; not a subclass, so its tests run once
    explain_and_ready = ser.Between.explain_and_ready
    escalate = ser.Trigger.escalate

    def reviewer(self, key, at):
        """A reviewer run of turn ``key`` at the head, created at ``at``."""
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,state,attempts,turn_key,"
                        "created,updated) VALUES(?,?,?,?,?,'reviewer','succeeded',0,?,?,?)",
                        (f"rev-{key}", f"drev-{key}", fg.REPO, 7, fg.HEAD, key, at, at))

    def superseded(self):
        """A shadow review of turn t1, then a newer reviewer turn with no review of its own."""
        now = time.time()
        self.reviewer("t1", now - 200)
        self.shadow("REQUEST_CHANGES", body="F1: the first review", at=now - 100)
        self.reviewer("reconsider:1", now - 50)

    def test_the_helper_tells_a_missing_review_from_an_unreadable_ledger(self):
        self.superseded()
        self.assertIs(turn_prompts.newest_shadow_review(ROW, missing=False), False)
        self.assertIsNone(turn_prompts.newest_shadow_review(ROW))       # the default is unchanged
        with mock.patch.object(views, "read_only_connect", return_value=None):
            self.assertIsNone(turn_prompts.newest_shadow_review(ROW, missing=False))

    def test_a_superseded_shadow_review_waits_for_the_active_turns_own(self):
        self.superseded()
        with self.assertRaises(run_supervisor.RetryableError) as caught:
            self.claim()
        self.assertEqual(str(caught.exception),
                         "waiting on the active reviewer turn's shadow review")
        self.assertTrue(run_supervisor.retryable(caught.exception))   # backoff, not failed (#2688)
        # ...and nothing was ruled on: the breach marker was never claimed for a ruling.
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")

    def test_an_unreadable_ledger_at_launch_still_waits_for_a_read(self):
        self.superseded()
        with mock.patch.object(views, "read_only_connect", return_value=None):
            with self.assertRaises(run_supervisor.RetryableError) as caught:
                self.claim()
        self.assertEqual(str(caught.exception), "shadow review unreadable before launch")

    def own_review(self, verdict, body, key="reconsider:1"):
        """The active (``key``) reviewer turn's own shadow review, landing during the wait."""
        self.shadow(verdict, body=body, at=time.time())
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET turn_key=? WHERE id=?", (key, f"s{self.n}"))

    def run_turn(self, during):
        """The ruling's whole worker turn (``_run_production``) over the real launch, its failure
        recorded by the real finish; ``during()`` runs while the ledger still shows it running."""
        from diaktoros import observer
        sup, seen = run_supervisor.Supervisor(self.db), {}

        def turn(t):
            try:
                self.claim()
            finally:
                t.row, t.loop = self.adjudicator_row(), self.loop
                seen["during"] = during()
        with mock.patch.object(run_supervisor.Supervisor, "_production_turn", side_effect=turn), \
                mock.patch.object(run_supervisor.Supervisor, "_refresh_check"), \
                mock.patch.object(observer, "notify", return_value=True):
            sup._run_production(self.claim_id(), "w")
        return seen

    def claim_id(self):
        self.mark_escalated(status="awaiting-adjudication")
        sup = run_supervisor.Supervisor(self.db)
        sup.enqueue(f"{fg.REPO}:7:{fg.HEAD}:adjudicator:breach:1", fg.REPO, 7, fg.HEAD,
                    "adjudicator", turn_key="breach:1")
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state='running', owner='w', launch_intent=? "
                        "WHERE seat='adjudicator'", (time.time(),))
        return self.adjudicator_row()["id"]

    def adjudicator_row(self):
        with closing(sqlite3.connect(self.db)) as con:
            con.row_factory = sqlite3.Row
            return con.execute("SELECT * FROM runs WHERE seat='adjudicator'").fetchone()

    def escalate_for_real(self):
        """A later dissent's escalation through the real breach delivery and ledger enqueue."""
        from diaktoros import gate, gh, observer
        sup = run_supervisor.Supervisor(self.db, fixture_mode=True)
        with mock.patch.object(gate, "isolated_supervisor", return_value=sup), \
                mock.patch.object(run_supervisor.Supervisor, "_spawn"), \
                mock.patch.object(gh, "pr", return_value=dict(fg.LIVE)), \
                mock.patch.object(observer, "notify", return_value=True):
            return gate.shadows.shadow_escalation(self.loop, self.st, 7, fg.HEAD, ser.APPROVED,
                                                  db=self.db)

    def test_an_agreeing_review_during_the_wait_is_not_ruled_on(self):
        self.superseded()
        self.own_review("APPROVE", "agrees now")
        with mock.patch.object(type(self.st), "breach_start") as start:
            with self.assertRaises(ValueError) as caught:
                self.claim()
        self.assertEqual(str(caught.exception), run_supervisor.turns.SHADOW_AGREES)
        self.assertFalse(run_supervisor.retryable(caught.exception))   # failed, not waited on
        start.assert_not_called()                  # the breach was never claimed for a ruling
        self.assertEqual(self.escalate()[0], "shadow agrees")

    def test_a_later_dissent_is_ruled_on_even_while_the_agreeing_run_ends(self):
        # #2688 review F1: the hold stays until the ledger shows the agreeing run failed. A
        # dissent before then finds it ("already"), not a fresh marker parked on that run's key;
        # one after re-arms the failed run through the real delivery, so it is ruled on.
        self.superseded()
        self.own_review("APPROVE", "agrees now")

        def dissent():
            self.reviewer("reconsider:2", time.time())
            self.own_review("REQUEST_CHANGES", "F3: a later dissent", key="reconsider:2")
            return (self.adjudicator_row()["state"], self.escalate_for_real())
        seen = self.run_turn(dissent)
        self.assertEqual(seen["during"], ("running", "already"))
        row = self.adjudicator_row()
        self.assertEqual(row["state"], "failed")
        self.assertIn(run_supervisor.turns.SHADOW_AGREES, row["error"])
        self.assertEqual(self.st.breach_get(7), {})     # its hold went with it, once it failed
        _marker, blockers, why = self.explain_and_ready()
        self.assertFalse(any(w.startswith("escalated:") for w in [*blockers, *why]), (blockers, why))
        self.assertEqual(self.escalate_for_real(), "new")
        self.assertEqual(self.adjudicator_row()["state"], "pending")      # re-armed for its ruling
        self.assertEqual(self.st.breach_get(7)["status"], "awaiting-adjudication")

    def test_only_this_rounds_shadow_escalation_awaiting_its_ruling_is_cleared(self):
        clear = self.st.breach_clear_pending
        for status, by, rounds, cleared in (("awaiting-adjudication", "shadow", 1, True),
                                            ("adjudicating", "shadow", 1, False),
                                            ("awaiting-adjudication", "dispute", 1, False),
                                            ("awaiting-adjudication", "shadow", 2, False)):
            with self.subTest(status=status, by=by, rounds=rounds):
                self.mark_escalated(by=by, status=status)
                self.assertEqual(clear(7, fg.HEAD, rounds), cleared)
                self.assertEqual(self.st.breach_get(7) == {}, cleared)
        self.mark_escalated(status="awaiting-adjudication")
        self.assertFalse(clear(7, fg.HEAD))         # without a round: delivery-pending only

    def test_the_active_turns_own_review_launches_as_before(self):
        self.superseded()
        self.own_review("REQUEST_CHANGES", "F2: the new turn's review")
        launch, _row = self.claim()
        self.assertIn("F2: the new turn's review", launch.prompt)
        self.assertNotIn("F1: the first review", launch.prompt)
        self.assertEqual(getattr(launch.scope, "shadow_run", None), f"s{self.n}")


if __name__ == "__main__":
    unittest.main()
