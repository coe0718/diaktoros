"""#1647: the shadow review follows the reviewer's APPROVE (``shadow_queue``).

A sampled head's shadow is queued once the reviewer's APPROVE there is confirmed, not beside the
reviewer's turn: on a head the reviewer sent back, its verdict decides nothing. It stays blind to
that approval, the merge check is never green between the APPROVE and its row, and the watchdog
queues one the broker's call missed. The ledger is real; GitHub and the worker launch are mocked.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import broker_ipc, gate, shadow_queue  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

REPO, HEAD = "acme/widgets", "a" * 40
LOOP = {"id": "w", "repo": REPO, "base": "main", "reviewers": ["rev"], "fixers": ["fix"],
        "shadow": {"profile": "shadow", "every": 1, "escalate": True}}


class Ledger(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "runs.sqlite"
        self.sup = Supervisor(self.db, fixture_mode=True)
        self.sup._spawn = lambda: None

    def run_row(self, seat, *, turn_key="", created=None, pr=7, head=HEAD):
        delivery = f"{REPO}:{pr}:{head}:{seat}" + (f":{turn_key}" if turn_key else "")
        self.sup.enqueue(delivery, REPO, pr, head, seat, turn_key=turn_key)
        with closing(sqlite3.connect(self.db)) as con, con:
            row_id = con.execute("SELECT id FROM runs WHERE delivery=?", (delivery,)).fetchone()[0]
            if created is not None:
                con.execute("UPDATE runs SET created=? WHERE id=?", (created, row_id))
        return row_id

    def receipt(self, run_id, verdict, state="confirmed"):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,"
                        "review_id,verdict,created,confirmed) VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, state, "g", 1, 9, verdict, time.time(), time.time()))


class Hold(Ledger):
    """No green gap: from the APPROVE until the shadow's row exists, the head waits for it."""

    def state(self):
        return gate.shadow_state(LOOP, 7, HEAD, db=self.db)

    def test_an_approved_head_with_no_shadow_row_yet_is_pending(self):
        self.receipt(self.run_row("reviewer"), "APPROVED")
        kind, hold, _note = self.state()
        self.assertEqual(kind, "pending")
        self.assertIn("its shadow review is being queued", hold)

    def test_once_the_shadow_is_queued_it_is_waited_on_as_before(self):
        self.receipt(self.run_row("reviewer"), "APPROVED")
        self.run_row("shadow")
        kind, hold, _note = self.state()
        self.assertEqual(kind, "pending")
        self.assertIn("waiting on the shadow review", hold)

    def test_a_head_sent_back_or_not_confirmed_or_not_sampled_waits_for_no_shadow(self):
        self.receipt(self.run_row("reviewer"), "CHANGES_REQUESTED")
        self.assertEqual(self.state()[:2], ("", ""))
        other = self.run_row("reviewer", turn_key="reconsider:1")
        self.receipt(other, "APPROVED", state="claimed")       # the APPROVE is not confirmed
        self.assertEqual(self.state()[:2], ("", ""))
        self.receipt(self.run_row("reviewer", turn_key="reconsider:2"), "APPROVED")
        with mock.patch.object(gate, "shadow_sampled", return_value=False):
            self.assertEqual(self.state()[:2], ("", ""))


class Broker(unittest.TestCase):
    """The broker queues the shadow right after the reviewer's APPROVE posts."""

    def after_review(self, verdict):
        fake = types.SimpleNamespace(_loop=LOOP, scope=types.SimpleNamespace(
            number=7, head=HEAD, run_id="r1", ledger_db="/ledger"))
        with mock.patch.object(shadow_queue, "after_approval") as after, \
                mock.patch("diaktoros.findings.record"), \
                mock.patch("diaktoros.merge_check.refresh_quietly") as refresh:
            broker_ipc.RunBroker._after_review(fake, verdict, "body")
        refresh.assert_called_once_with(LOOP, 7)
        return after

    def test_an_approve_queues_the_shadow(self):
        self.after_review("APPROVE").assert_called_once_with(LOOP, 7, HEAD, "r1", "/ledger")

    def test_a_change_request_queues_none(self):
        self.after_review("REQUEST_CHANGES").assert_not_called()


class AfterApproval(Ledger):
    def test_the_shadow_takes_the_reviewers_turn_key(self):
        run = self.run_row("reviewer", turn_key="reconsider:3")
        with mock.patch.object(gate, "isolated_supervisor", return_value="sup"), \
                mock.patch.object(gate, "enqueue_shadow", return_value="enqueued") as enqueue:
            self.assertEqual(shadow_queue.after_approval(LOOP, 7, HEAD, run, self.db), "enqueued")
        enqueue.assert_called_once_with(LOOP, "sup", 7, HEAD, turn_key="reconsider:3")

    def test_a_failure_never_raises(self):
        with mock.patch.object(gate, "isolated_supervisor", side_effect=OSError("no runtime")):
            self.assertEqual(shadow_queue.after_approval(LOOP, 7, HEAD, None, self.db), "error")


class Sweep(Ledger):
    """The watchdog's backstop for a shadow the broker's call after the APPROVE did not queue."""

    PR = {"number": 7, "head": {"sha": HEAD, "ref": "b"}, "user": {"login": "fix"},
          "base": {"ref": "main"},
          "state": "open", "draft": False}

    def sweep(self):
        with mock.patch.object(gate, "isolated_supervisor", return_value="sup"), \
                mock.patch.object(gate, "enqueue_shadow", return_value="enqueued") as enqueue:
            queued = shadow_queue.sweep(LOOP, [self.PR], db=self.db)
        return queued, enqueue

    def test_an_approved_head_without_a_shadow_gets_one(self):
        self.receipt(self.run_row("reviewer", turn_key="k"), "APPROVED")
        queued, enqueue = self.sweep()
        self.assertEqual(queued, [7])
        enqueue.assert_called_once_with(LOOP, "sup", 7, HEAD, turn_key="k")

    def test_nothing_when_the_shadow_exists_or_the_head_was_sent_back(self):
        self.receipt(self.run_row("reviewer"), "CHANGES_REQUESTED")
        self.assertEqual(self.sweep()[0], [])
        self.receipt(self.run_row("reviewer", turn_key="k"), "APPROVED")
        self.run_row("shadow", turn_key="k")
        queued, enqueue = self.sweep()
        self.assertEqual(queued, [])
        enqueue.assert_not_called()

    def test_off_without_a_shadow_profile(self):
        self.receipt(self.run_row("reviewer"), "APPROVED")
        with mock.patch.object(gate, "enqueue_shadow") as enqueue:
            self.assertEqual(shadow_queue.sweep({**LOOP, "shadow": {}}, [self.PR], db=self.db), [])
        enqueue.assert_not_called()


class Blind(Ledger):
    """The shadow's prompt shows only the reviews posted before the reviewer was queued."""

    def test_the_reviewers_approval_is_never_shown_to_its_shadow(self):
        queued = time.time() - 600
        self.run_row("reviewer", created=queued)
        shadow = self.run_row("shadow", created=queued + 300)   # queued after the APPROVE
        row = {"repo": REPO, "pr": 7, "head": HEAD, "turn_key": "", "created": queued + 300,
               "id": shadow}
        stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
        earlier = {"id": 1, "state": "CHANGES_REQUESTED", "submitted_at": stamp(queued - 60)}
        approve = {"id": 2, "state": "APPROVED", "submitted_at": stamp(queued + 200)}
        self.assertEqual(shadow_queue.shown_reviews(row, [earlier, approve], self.db), [earlier])

    def test_without_a_reviewer_row_its_own_queueing_time_is_the_cutoff(self):
        row = {"repo": REPO, "pr": 7, "head": HEAD, "turn_key": "", "created": time.time() - 100}
        late = {"id": 3, "state": "APPROVED",
                "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.assertEqual(shadow_queue.shown_reviews(row, [late], self.db), [])


if __name__ == "__main__":
    unittest.main()
