"""#2320: a fixer answers row written 'denied' proves nothing was posted, so it is no write record.

``begin_answers`` writes 'denied' only when ``broker.authorize`` raised, before any POST (a refusal,
or a transient error during its live reads). Recovery used to count it as a possible write: a
cancel quarantined the run as uncertain and ``retry`` refused it. Now it counts for nothing, and a
re-armed run can still post its answers (the 'denied' row is replaced, never a second comment).
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import dataclasses
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_answers as base  # noqa: E402
from diaktoros import observer, run_supervisor  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402
from diaktoros.state import ledger  # noqa: E402

WHY = "The runner was shut down mid-job; nothing in this PR's code fails.\n"


class DeniedAnswers(base.Base):
    def setUp(self):
        super().setUp()
        self.scope = dataclasses.replace(self.scope, ci_fix=True)
        patch = mock.patch.object(observer, "notify")
        patch.start()
        self.addCleanup(patch.stop)

    def records(self):
        with Supervisor(self.db)._connect() as con:
            return run_supervisor.write_records(con, self.run_id)

    def decline(self, authorize=None):
        """The CI fix's decline through the real broker; ``authorize`` replaces the live check."""
        patch = (mock.patch.object(base.broker_ipc.broker, "authorize", side_effect=authorize)
                 if authorize else contextlib.nullcontext())
        with patch, self.serving() as server:
            return self.send(server, {"operation": "request_review", "body": WHY,
                                      "verdict": "DECLINE:not-this-pr"}), server.completed

    def fail_run(self):
        """The worker ends the turn the broker never completed: failed, no automatic retry."""
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET owner='w' WHERE id=?", (self.run_id,))
        return Supervisor(self.db).complete_uncertain(
            self.run_id, "w", 0, "agent exited without a confirmed scoped write", retry=False)

    def test_each_answers_state_but_denied_is_a_write_record(self):
        for state in ("posting", "posted", "uncertain", "denied"):
            with self.subTest(state=state), ledger.connect(self.db) as con:
                con.execute("DELETE FROM fixer_answers")
                con.execute("INSERT INTO fixer_answers(run_id,repo,pr,base,head,body,state,"
                            "created,updated) VALUES(?,?,7,?,?,'x',?,?,?)",
                            (self.run_id, base.REPO, base.HEAD, base.HEAD, state, time.time(),
                             time.time()))
                con.commit()
                self.assertEqual(self.records(),
                                 None if state == "denied" else f"fixer answers {state}")

    def test_a_denied_or_failed_authorization_leaves_the_run_retryable(self):
        refusals = (base.broker_ipc.broker.BrokerDenied("not the fixer"), OSError("reset"))
        for refusal in refusals:
            with self.subTest(refusal=type(refusal).__name__):
                self.setUp()
                answered, completed = self.decline(refusal)
                self.assertIn("answers comment denied", answered["error"])
                self.assertFalse(completed)
                self.assertEqual(self.fake.posts(), [])
                with ledger.connect(self.db) as con:
                    self.assertEqual(con.execute("SELECT state FROM fixer_answers").fetchone()[0],
                                     "denied")
                self.assertEqual((self.fail_run(), self.records()), ("failed", None))
                sup = Supervisor(self.db)
                self.assertEqual(sup.retry(self.run_id), "pending")
                self.assertEqual(sup.cancel(self.run_id)["state"], "cancelled")   # not uncertain

    def test_a_re_armed_run_still_posts_its_answers_once(self):
        answered, _ = self.decline(base.broker_ipc.broker.BrokerDenied("not the fixer"))
        self.assertFalse(answered["ok"])
        self.fail_run()
        self.assertEqual(Supervisor(self.db).retry(self.run_id), "pending")
        with ledger.connect(self.db) as con:           # the worker claims and launches it again
            con.execute("UPDATE runs SET state='running',launch_intent=1 WHERE id=?",
                        (self.run_id,))
        answered, completed = self.decline()
        self.assertEqual(answered["result"]["answers"], "posted", answered)
        self.assertTrue(completed)
        self.assertEqual(len(self.fake.posts()), 1)
        self.assertEqual(self.records(), "CI-fix decline recorded")
        with ledger.connect(self.db) as con:
            self.assertEqual(con.execute("SELECT state FROM fixer_answers").fetchall(),
                             [("posted",)])


if __name__ == "__main__":
    unittest.main()
