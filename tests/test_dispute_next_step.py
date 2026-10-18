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


if __name__ == "__main__":
    unittest.main()
