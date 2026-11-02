"""#1647: a shadow review runs only on a head its own reviewer turn approved (``shadow_queue``).

The shadow's row is queued with its reviewer turn, on a findings snapshot taken before that
turn's row exists, so the head holds for it from the start. As it is claimed it reads, from the
ledger alone, what that very reviewer turn decided: a confirmed APPROVE runs it, a change request
or a turn that ended without a verdict skips it, and anything still in flight waits. The ledger is
real; GitHub and the worker launch are mocked.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import ci, gate, merge_check, ready, shadow_queue, state as state_mod  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

REPO, HEAD = "acme/widgets", "a" * 40


class Ledger(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = pathlib.Path(tmp.name)
        self.db = self.tmp / "runs.sqlite"
        self.sup = Supervisor(self.db, fixture_mode=True)
        self.sup._spawn = lambda: None

    def run_row(self, seat, *, turn_key="", state="pending", pr=7, head=HEAD):
        delivery = f"{REPO}:{pr}:{head}:{seat}" + (f":{turn_key}" if turn_key else "")
        self.sup.enqueue(delivery, REPO, pr, head, seat, turn_key=turn_key)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET state=? WHERE delivery=?", (state, delivery))
            return con.execute("SELECT id FROM runs WHERE delivery=?", (delivery,)).fetchone()[0]

    def receipt(self, run_id, verdict, state="confirmed"):
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,"
                        "review_id,verdict,created,confirmed) VALUES(?,?,?,?,?,?,?,?)",
                        (run_id, state, "g", 1, 9, verdict, time.time(), time.time()))

    def decide(self, turn_key=""):
        row = {"repo": REPO, "pr": 7, "head": HEAD, "turn_key": turn_key}
        return shadow_queue.after_reviewer(self.db, row)


class AfterReviewer(Ledger):
    def test_a_confirmed_approve_runs_it(self):
        self.receipt(self.run_row("reviewer", state="succeeded"), "APPROVED")
        self.assertEqual(self.decide(), ("run", ""))

    def test_a_change_request_or_a_turn_with_no_verdict_skips_it(self):
        self.receipt(self.run_row("reviewer", state="succeeded"), "CHANGES_REQUESTED")
        self.assertEqual(self.decide()[0], "skip")
        self.assertIn("requested changes", self.decide()[1])
        self.run_row("reviewer", turn_key="r2", state="failed")
        what, why = self.decide("r2")
        self.assertEqual(what, "skip")
        self.assertIn("ended failed without a verdict", why)

    def test_a_turn_in_flight_its_post_or_an_uncertain_record_waits(self):
        for key, state, receipt in (("a", "running", None), ("b", "running", "claimed"),
                                    ("c", "uncertain", None), ("d", "succeeded", "posted")):
            with self.subTest(state=state, receipt=receipt):
                run = self.run_row("reviewer", turn_key=key, state=state)
                if receipt:
                    self.receipt(run, "APPROVE", state=receipt)
                self.assertEqual(self.decide(key)[0], "wait")

    def test_only_its_own_reviewer_turn_counts(self):
        # Review of #1656, F2/F4: an older turn's APPROVE at the same head is not this turn's;
        # a newer change request after an older approval skips the newer turn's shadow.
        self.receipt(self.run_row("reviewer", state="succeeded"), "APPROVED")
        self.receipt(self.run_row("reviewer", turn_key="reconsider:1", state="succeeded"),
                     "CHANGES_REQUESTED")
        self.assertEqual(self.decide("reconsider:1")[0], "skip")
        self.run_row("reviewer", turn_key="reconsider:2", state="running")
        self.assertEqual(self.decide("reconsider:2")[0], "wait")

    def test_an_unreadable_ledger_waits_and_a_row_from_before_runs(self):
        with mock.patch("diaktoros.run_supervisor.views.read_only_connect", return_value=None):
            self.assertEqual(self.decide()[0], "wait")
        self.assertEqual(self.decide(), ("run", ""))      # no reviewer row: queued before #1647


class SnapshotFirst(Ledger):
    """Review of #1656, F6: the snapshot is taken before the reviewer's row exists, so a worker
    that records the review's findings as soon as it is spawned can never be in it."""

    def test_a_worker_recording_findings_before_submit_returns_is_not_in_the_snapshot(self):
        loop = {"id": "w", "repo": REPO, "state_dir": str(self.tmp / "state"),
                "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        sup = mock.Mock()
        def submit(delivery, *_a, **_k):
            if delivery.endswith(":reviewer"):
                st.findings_put(7, {"findings": {"F1": {"state": "open"}}})   # the worker ran
            return "enqueued"
        sup.submit.side_effect = submit
        with mock.patch.object(gate, "isolated_supervisor", return_value=sup), \
                mock.patch.object(gate, "shadow_open_heads", return_value=set()):
            self.assertEqual(gate.enqueue_isolated(loop, "reviewer", 7, HEAD), "enqueued")
        self.assertEqual(st.shadow_findings_get(7, HEAD), {})
        self.assertEqual([c.args[4] for c in sup.submit.call_args_list], ["reviewer", "shadow"])


class NeverGreenInTheGap(Ledger):
    """Review of #1656, F1/F7: through ``ready`` and the merge check, on the real ledger. While
    the reviewer's APPROVE is being posted (GitHub already shows it), its shadow's row is open, so
    the head holds; it is green only once the shadow's review is in."""

    LOOP = {"id": "w", "repo": REPO, "base": "main", "cap": 3, "read_token": "rev",
            "reviewers": ["rev"], "reviewer_seat": "rev", "fixers": ["fix"], "review_only": [],
            "required_checks": ["tests"], "cooldown_h": 6,
            "seats": {"reviewer": {"route": "w-review"}, "fixer": {"route": "w-fix"}},
            "shadow": {"profile": "shadow", "every": 1, "escalate": True}}

    def kinds(self):
        loop = {**self.LOOP, "state_dir": str(self.tmp / "state")}
        pr = {"number": 7, "draft": False, "user": {"login": "fix"}, "base": {"ref": "main"},
              "head": {"sha": HEAD, "ref": "b7"}}
        approval = {"id": 1, "state": "APPROVED", "commit_id": HEAD,
                    "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "rev", "id": 7}}
        detail = {"mergeable": True, "mergeable_state": "clean", "number": 7, "state": "open",
                  "draft": False, "head": {"sha": HEAD}, "base": {"ref": "main"}}
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=self.db), \
                mock.patch.object(ready.gate_failures, "open_for", return_value=[]):
            return ready.assess_kinds(loop, state_mod.state_for(loop), pr, [approval],
                                      ci.CIState(passed=["tests"]), detail)

    def test_the_head_holds_from_the_post_until_the_shadow_lands(self):
        reviewer = self.run_row("reviewer", state="running")
        self.receipt(reviewer, "APPROVE", state="claimed")      # posted, not yet confirmed
        self.run_row("shadow", state="pending")                 # queued with the reviewer
        state, _summary = merge_check.decide(self.kinds())
        self.assertEqual(state, merge_check.PENDING)
        self.assertTrue(any("shadow review" in why for _kind, why in self.kinds()))


if __name__ == "__main__":
    unittest.main()
