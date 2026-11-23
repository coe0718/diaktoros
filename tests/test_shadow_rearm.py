"""A re-armed reviewer turn owes its shadow row too (#2072, #2076).

``Supervisor.submit`` wrote the shadow row only beside a *new* reviewer row (#1682). A redelivery
that re-armed a failed reviewer row with no shadow row (a row from before #1682 whose shadow
submit failed for good, or one first queued while the loop was unsampled) left the head with no
shadow, so ``shadow.escalate`` read "nothing to wait for" while the re-armed reviewer could
approve. The re-arm now writes the missing shadow row in its own transaction.

The same holds for a redelivery that finds the reviewer row still to start (``pending``, or
``waiting``; #2627) and for ``hermes dk retry`` (``Supervisor.retry``, #2628): each leaves the
reviewer row with the shadow row it owes, on a findings snapshot kept then.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import config, gate  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

HEAD = "a" * 40
RAW = {"id": "widgets", "repo": "acme/widgets", "fixers": ["fixer"], "reviewers": ["reviewer"],
       "read_token": "reader",
       "seats": {"reviewer": {"profile": "rev", "route": "r"},
                 "fixer": {"profile": "fix", "route": "f"}}}
REVIEWER = f"acme/widgets:8:{HEAD}:reviewer"
SHADOW = f"acme/widgets:8:{HEAD}:shadow"
ESCALATE = {"profile": "grok", "every": 1, "escalate": True}


class _Ledger(unittest.TestCase):
    """A temp ledger and runtime, ``_spawn`` patched; no tests of its own."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = pathlib.Path(temp.name)
        runtime = self.home / "diaktoros-runtime.json"
        runtime.write_text(json.dumps({"source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}))
        runtime.chmod(0o600)
        patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                             "DIAKTOROS_CONFIG_DIR": str(self.home / "none")})
        patch.start()
        self.addCleanup(patch.stop)
        spawn = mock.patch.object(Supervisor, "_spawn")       # no real worker into the temp dir
        spawn.start()
        self.addCleanup(spawn.stop)

    def loop(self, shadow=None):
        return config.normalize({**RAW, "state_dir": str(self.home / "state"),
                                 "required_checks": ["tests"], **({"shadow": shadow} if shadow else {})})

    def enqueue(self, loop):
        with mock.patch.object(config, "by_repo", return_value=loop):
            return gate.enqueue_isolated(loop, "reviewer", 8, HEAD)

    def sup(self):
        return Supervisor(self.home / "state" / "diaktoros-runs.sqlite",
                          production_config=self.home / "diaktoros-runtime.json",
                          hermes_home=self.home)

    def fail_reviewer(self, *, drop_shadow=False):
        with closing(sqlite3.connect(self.sup().db)) as con, con:
            con.execute("UPDATE runs SET state='failed' WHERE delivery=?", (REVIEWER,))
            if drop_shadow:
                con.execute("DELETE FROM runs WHERE delivery=?", (SHADOW,))


class RearmedReviewerShadow(_Ledger):
    def test_sampling_turned_on_before_the_redelivery_queues_the_shadow(self):
        # #2076's probe: unsampled first enqueue, the reviewer row fails, then the redelivery
        # under shadow.escalate re-arms it.
        self.assertEqual(self.enqueue(self.loop()), "enqueued")
        self.assertIsNone(self.sup().get(SHADOW))
        self.fail_reviewer()
        loop = self.loop(ESCALATE)
        self.assertEqual(self.enqueue(loop), "rearmed")
        sup = self.sup()
        self.assertEqual(sup.get(REVIEWER)["state"], "pending")
        self.assertEqual(sup.get(SHADOW)["state"], "pending")
        kind, hold, note = gate.shadows.shadow_state(loop, 8, HEAD, sup.db)
        self.assertEqual(kind, "pending", (hold, note))
        self.assertNotIn("nothing to wait for", note)

    def test_a_pre_upgrade_row_with_no_shadow_gets_one_when_re_armed(self):
        # #2072: a reviewer row from before #1682 whose shadow submit failed for good.
        loop = self.loop(ESCALATE)
        self.assertEqual(self.enqueue(loop), "enqueued")
        self.fail_reviewer(drop_shadow=True)
        self.assertEqual(self.enqueue(loop), "rearmed")
        self.assertEqual(self.sup().get(SHADOW)["state"], "pending")
        self.assertEqual(gate.shadows.shadow_state(loop, 8, HEAD, self.sup().db)[0], "pending")

    def test_a_shadow_row_that_cannot_be_written_re_arms_no_reviewer_either(self):
        # One transaction, as for a new row (#1682): the re-arm rolls back with the shadow's row.
        self.enqueue(self.loop())
        self.fail_reviewer()
        loop, budget = self.loop(ESCALATE), config.turn_budget
        with mock.patch.object(config, "turn_budget",
                               side_effect=lambda lp, seat: 0 if seat == "shadow"
                               else budget(lp, seat)), \
                self.assertRaisesRegex(ValueError, "shadow row"):
            self.enqueue(loop)
        self.assertEqual(self.sup().get(REVIEWER)["state"], "failed")
        self.assertIsNone(self.sup().get(SHADOW))

    def test_an_existing_shadow_row_is_left_as_it_is(self):
        # Guard: a re-arm never writes a second shadow row beside one already there.
        loop = self.loop(ESCALATE)
        self.enqueue(loop)
        self.fail_reviewer()
        self.assertEqual(self.enqueue(loop), "rearmed")
        with closing(sqlite3.connect(self.sup().db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM runs WHERE seat='shadow'")
                             .fetchone()[0], 1)


class UnclaimedAndRetriedReviewerShadow(_Ledger):
    """#2627 (a redelivery finds the reviewer row still to start) and #2628 (``retry``)."""

    def shadow_rows(self):
        with closing(sqlite3.connect(self.sup().db)) as con:
            return con.execute("SELECT state FROM runs WHERE seat='shadow'").fetchall()

    def assert_holds(self, loop):
        self.assertEqual(self.sup().get(SHADOW)["state"], "pending")
        kind, hold, note = gate.shadows.shadow_state(loop, 8, HEAD, self.sup().db)
        self.assertEqual(kind, "pending", (hold, note))
        self.assertNotIn("nothing to wait for", note)

    def retry(self, loop):
        # As `hermes dk retry` builds it: the ledger alone, no production configuration.
        with mock.patch.object(config, "by_repo", return_value=loop):
            return Supervisor(self.sup().db).retry(self.sup().get(REVIEWER)["id"])

    def test_a_redelivery_that_finds_the_reviewer_pending_queues_its_shadow(self):
        # #2627's probe: first queued unsampled, still pending when escalate is turned on.
        self.assertEqual(self.enqueue(self.loop()), "enqueued")
        loop = self.loop(ESCALATE)
        self.assertEqual(self.enqueue(loop), "pending")
        self.assertEqual(self.sup().get(REVIEWER)["state"], "pending")
        self.assert_holds(loop)

    def test_a_redelivery_that_finds_the_reviewer_waiting_queues_its_shadow(self):
        self.enqueue(self.loop())
        with closing(sqlite3.connect(self.sup().db)) as con, con:
            con.execute("UPDATE runs SET state='waiting', retry_at=1e12, retries=1 "
                        "WHERE delivery=?", (REVIEWER,))
        loop = self.loop(ESCALATE)
        self.assertTrue(self.enqueue(loop).startswith("duplicate waiting"))
        self.assertEqual(self.sup().get(REVIEWER)["state"], "waiting")
        self.assert_holds(loop)

    def test_a_pending_redelivery_unsampled_writes_no_shadow(self):
        self.enqueue(self.loop())
        self.assertEqual(self.enqueue(self.loop()), "pending")
        self.assertEqual(self.shadow_rows(), [])

    def test_retry_re_arms_the_reviewer_with_the_shadow_it_owes(self):
        # #2628's probe: a pre-#1682 row whose shadow submit failed for good, then failed.
        loop = self.loop(ESCALATE)
        self.assertEqual(self.enqueue(loop), "enqueued")
        self.fail_reviewer(drop_shadow=True)
        self.assertEqual(self.retry(loop), "pending")
        self.assertEqual(self.sup().get(REVIEWER)["state"], "pending")
        self.assert_holds(loop)
        st = gate.state_mod.state_for(loop)
        self.assertIsNotNone(st.shadow_findings_get(8, HEAD, ""))

    def test_retry_of_a_reviewer_first_queued_unsampled_queues_its_shadow(self):
        self.enqueue(self.loop())
        self.fail_reviewer()
        loop = self.loop(ESCALATE)
        self.assertEqual(self.retry(loop), "pending")
        self.assert_holds(loop)

    def test_retry_unsampled_or_beside_a_shadow_row_writes_none(self):
        self.enqueue(self.loop())
        self.fail_reviewer()
        self.assertEqual(self.retry(self.loop()), "pending")
        self.assertEqual(self.shadow_rows(), [])
        # A shadow row already there (here with no snapshot of its own, #1139) is left alone.
        loop = self.loop(ESCALATE)
        self.fail_reviewer()
        with closing(sqlite3.connect(self.sup().db)) as con, con:
            con.execute("INSERT INTO runs(id,delivery,repo,pr,head,seat,turn_key,state,created,"
                        "updated,push_admitted,budget) SELECT 'sh',?,repo,pr,head,'shadow',"
                        "turn_key,'failed',created,updated,0,budget FROM runs WHERE delivery=?",
                        (SHADOW, REVIEWER))
        self.assertEqual(self.retry(loop), "pending")
        self.assertEqual(self.shadow_rows(), [("failed",)])

    def test_a_shadow_row_that_cannot_be_written_refuses_the_retry(self):
        self.enqueue(self.loop())
        self.fail_reviewer()
        loop, budget = self.loop(ESCALATE), config.turn_budget
        with mock.patch.object(config, "turn_budget",
                               side_effect=lambda lp, seat: 0 if seat == "shadow"
                               else budget(lp, seat)), \
                self.assertRaisesRegex(ValueError, "shadow row"):
            self.retry(loop)
        self.assertEqual(self.sup().get(REVIEWER)["state"], "failed")
        self.assertEqual(self.shadow_rows(), [])


if __name__ == "__main__":
    unittest.main()
