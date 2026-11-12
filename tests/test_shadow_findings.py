"""A shadow review is judged on the findings ledger it was queued with (#218, seen on #1005).

The shadow is queued beside the real reviewer turn and shown only the reviews posted before then.
It runs later (after CI), by which time the real review of the same head may have recorded new
findings. The broker judged the shadow against that live ledger: on #1005 the reviewer's F1, which
the shadow never saw, refused its APPROVE ("cannot APPROVE with open finding(s) F1") and the
recorded shadow verdict became a forced block. The gate now snapshots the ledger when it queues the
shadow, and the no-write broker judges the shadow against that snapshot.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import dataclasses
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_selftest as ts  # noqa: E402
from diaktoros import broker, gate, review_receipt, state as state_mod  # noqa: E402
from diaktoros.broker import ipc as broker_ipc  # noqa: E402

VEX_SAME_ROUND = {"findings": {"F1": {"state": "open", "text": "a.py:1: x", "at": time.time()}},
                  "last_head": ts.HEAD}


class Snapshot(unittest.TestCase):
    setUp = ts.NoWriteBrokerTests.setUp
    serve = ts.NoWriteBrokerTests.serve

    def approve(self):
        server = self.serve(require_receipt=True, no_write=True)
        with mock.patch.object(broker, "perform", side_effect=AssertionError), \
                mock.patch.object(review_receipt, "submit", side_effect=AssertionError):
            return broker_ipc.request("review", verdict="APPROVE", body="LGTM\nNot verified: nothing",
                                      socket_path=str(server.socket_path)), server

    def test_a_shadow_is_judged_on_the_ledger_it_was_queued_with(self):
        self.scope = dataclasses.replace(self.scope, shadow=True)
        st = state_mod.state_for(self.fx.loop)
        st.findings_put(7, VEX_SAME_ROUND)               # the real review, meanwhile
        st.shadow_findings_put(7, ts.HEAD, {})           # what the shadow was queued with
        answer, server = self.approve()
        self.assertTrue(answer.get("ok"), answer)
        self.assertEqual(server.recorded[0]["verdict"], "APPROVE")

    def test_without_a_snapshot_the_live_ledger_still_applies(self):
        # Another no-write broker (selftest's live turn, the corpus) has no shadow snapshot: it
        # keeps the live ledger. A shadow's own refuses instead (Retention, #1139).
        state_mod.state_for(self.fx.loop).findings_put(7, VEX_SAME_ROUND)
        answer, _ = self.approve()
        self.assertFalse(answer.get("ok"))
        self.assertIn("cannot APPROVE with open finding(s) F1", answer.get("error", ""))

    def test_queueing_takes_the_snapshot_once(self):
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        supervisor = mock.Mock()
        gate.enqueue_shadow(loop, supervisor, 7, ts.HEAD)
        supervisor.submit.assert_called_once()
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})
        st.findings_put(7, VEX_SAME_ROUND)
        gate.enqueue_shadow(loop, supervisor, 7, ts.HEAD)  # a redelivery after the review
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})    # the first one stands
        self.assertIsNone(st.shadow_findings_get(7, "b" * 40))


class Retention(unittest.TestCase):
    """A snapshot outlives its shadow run: the 300-entry cap never evicts one in use (#1013)."""

    setUp = ts.NoWriteBrokerTests.setUp
    serve = ts.NoWriteBrokerTests.serve

    def fill(self, st, n):
        for i in range(n):
            st.shadow_findings_put(1000 + i, ts.HEAD, {})

    def ledger(self, *runs):
        """A run ledger holding shadow rows ``(pr, state)`` or ``(pr, state, repo)`` (default
        ``acme/widgets``, the fixture loop's), as the production one would."""
        import pathlib
        import sqlite3
        import tempfile
        from contextlib import closing
        from diaktoros.run_supervisor import Supervisor
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db = pathlib.Path(temp.name) / "runs.sqlite"
        sup = Supervisor(db, fixture_mode=True)
        sup._spawn = lambda: None
        for pr, state, *repo in runs:
            repo = repo[0] if repo else "acme/widgets"
            delivery = f"{repo}:{pr}:{ts.HEAD}:shadow"
            sup.enqueue(delivery, repo, pr, ts.HEAD, "shadow")
            with closing(sqlite3.connect(db)) as con, con:
                con.execute("UPDATE runs SET state=? WHERE delivery=?", (state, delivery))
        return db

    def test_the_cap_spares_a_snapshot_whose_shadow_is_still_open(self):
        st = state_mod.state_for(self.fx.loop)
        self.fill(st, st.SHADOW_FINDINGS_KEPT)
        st.shadow_findings_put(5, ts.HEAD, {}, {(1000, ts.HEAD)})
        self.assertEqual(st.shadow_findings_get(1000, ts.HEAD), {})   # the oldest, still in use
        self.assertIsNone(st.shadow_findings_get(1001, ts.HEAD))      # the oldest one that is not
        self.assertEqual(st.shadow_findings_get(5, ts.HEAD), {})

    def test_an_unknown_set_of_open_runs_evicts_nothing(self):
        st = state_mod.state_for(self.fx.loop)
        self.fill(st, st.SHADOW_FINDINGS_KEPT + 3)
        self.assertEqual(st.shadow_findings_get(1000, ts.HEAD), {})

    def test_a_ledger_path_that_is_not_a_file_evicts_nothing(self):
        # #1130: a directory at the ledger path is an existing ledger that cannot be read, not
        # "no open runs": the empty set would have let the cap evict the oldest snapshot, while
        # ``shadow_hold`` on the same path holds. Through the enforcing entry point.
        import tempfile
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        self.fill(st, st.SHADOW_FINDINGS_KEPT)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=temp.name):
            with self.assertRaises(OSError):
                gate.shadow_open_heads("acme/widgets")
            held, _note = gate.shadow_hold({**loop, "shadow": {**loop["shadow"], "escalate": True}},
                                           7, ts.HEAD)
            supervisor = mock.Mock()
            gate.enqueue_shadow(loop, supervisor, 7, ts.HEAD)
        self.assertTrue(held)                                          # the hold agrees
        self.assertEqual(st.shadow_findings_get(1000, ts.HEAD), {})   # the oldest: not evicted
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})
        supervisor.submit.assert_called_once()                        # the shadow is still queued

    def test_queueing_through_the_gate_keeps_the_snapshot_of_a_delayed_shadow(self):
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        self.fill(st, st.SHADOW_FINDINGS_KEPT)
        db = self.ledger((1000, "waiting"), (1001, "succeeded"))
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=db):
            gate.enqueue_shadow(loop, mock.Mock(), 7, ts.HEAD)
        self.assertEqual(st.shadow_findings_get(1000, ts.HEAD), {})   # delayed, still open
        self.assertIsNone(st.shadow_findings_get(1001, ts.HEAD))      # ended: evicted
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})

    def test_another_repos_open_shadow_at_the_same_pr_and_head_keeps_nothing(self):
        # #1133: the snapshot file is per loop and keyed "pr:head", so an open shadow run of
        # another repo at the same PR number and head must not hold this loop's snapshot.
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        self.fill(st, st.SHADOW_FINDINGS_KEPT)
        db = self.ledger((1000, "waiting", "other/repo"), (1001, "waiting"))
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=db):
            self.assertEqual(gate.shadow_open_heads("acme/widgets"), {(1001, ts.HEAD)})
            self.assertEqual(gate.shadow_open_heads("other/repo"), {(1000, ts.HEAD)})
            gate.enqueue_shadow(loop, mock.Mock(), 7, ts.HEAD)       # the enforcing entry point
        self.assertIsNone(st.shadow_findings_get(1000, ts.HEAD))      # only another repo's: evicted
        self.assertEqual(st.shadow_findings_get(1001, ts.HEAD), {})   # this repo's, open: kept
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})

    def test_a_delayed_shadow_is_still_judged_on_its_snapshot_after_the_cap(self):
        st = state_mod.state_for(self.fx.loop)
        st.findings_put(7, VEX_SAME_ROUND)
        st.shadow_findings_put(7, ts.HEAD, {}, set())
        self.fill(st, st.SHADOW_FINDINGS_KEPT)
        st.shadow_findings_put(8, ts.HEAD, {}, {(7, ts.HEAD)})
        answer, server = Snapshot.approve(self)
        self.assertTrue(answer.get("ok"), answer)

    def test_a_second_enqueue_in_the_gap_before_submit_cannot_evict_the_first_snapshot(self):
        """299 open snapshots; B's enqueue starts while A's run is not yet in the ledger."""
        import threading
        from diaktoros.run_supervisor import Supervisor
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        self.fill(st, st.SHADOW_FINDINGS_KEPT - 1)
        db = self.ledger(*[(1000 + i, "waiting") for i in range(st.SHADOW_FINDINGS_KEPT - 1)])
        sup = Supervisor(db, fixture_mode=True)
        sup._spawn = lambda: None
        seen = {}
        second = threading.Thread(target=lambda: seen.update(
            b=gate.enqueue_shadow(loop, sup, 8, ts.HEAD)))
        real_submit = sup.submit

        def submit_a(*args, **kwargs):
            if not second.is_alive() and not seen:
                second.start()              # B arrives after A's snapshot, before A's row
                second.join(0.5)            # unfixed, B finishes here and evicts A's snapshot
            return real_submit(*args, **kwargs)

        sup.submit = submit_a
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=db):
            a = gate.enqueue_shadow(loop, sup, 7, ts.HEAD)
            second.join(30)
        self.assertEqual(a, "enqueued")
        self.assertEqual(seen.get("b"), "enqueued")
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD), {})      # A's run is open: kept
        self.assertEqual(st.shadow_findings_get(8, ts.HEAD), {})

    def test_no_snapshot_no_shadow(self):
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        supervisor = mock.Mock()
        with mock.patch.object(state_mod.LoopState, "shadow_findings_put",
                               side_effect=OSError("disk full")):
            self.assertEqual(gate.enqueue_shadow(loop, supervisor, 7, ts.HEAD), "no snapshot")
        supervisor.submit.assert_not_called()

    def queued_failed_shadow(self, loop, turn_key="", production=True):
        """A failed shadow row with no findings snapshot on file (evicted), and a real supervisor
        over its ledger; ``production_ledger`` is patched to that ledger as the gate reads it
        (``production=False``: to an empty one, so only the supervisor's own ledger holds it)."""
        import sqlite3
        import tempfile
        from contextlib import closing
        from diaktoros.run_supervisor import Supervisor
        db = self.ledger((7, "failed"))
        if turn_key:
            with closing(sqlite3.connect(db)) as con, con:
                con.execute("UPDATE runs SET turn_key=?, delivery=delivery||?",
                            (turn_key, ":" + turn_key))
        sup = Supervisor(db, fixture_mode=True)
        sup._spawn = lambda: None
        elsewhere = db
        if not production:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            elsewhere = Path(temp.name) / "production.sqlite"      # no ledger there at all
        patcher = mock.patch("diaktoros.run_supervisor.production_ledger", return_value=elsewhere)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertIsNone(state_mod.state_for(loop).shadow_findings_get(7, ts.HEAD))
        return sup

    def test_a_new_turns_snapshot_does_not_stand_in_for_an_evicted_one(self):
        # Review of #1900, F1: snapshots are keyed by PR and head, so a new reviewer turn at the
        # head takes the evicted place. The old turn's failed row must not be re-armed on it,
        # neither by the gate's enqueue nor by the operator's retry.
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        sup = self.queued_failed_shadow(loop, turn_key="old")
        st.findings_put(7, VEX_SAME_ROUND)                  # live findings, since the old turn
        self.assertEqual(gate.enqueue_shadow(loop, sup, 7, ts.HEAD, turn_key="new"), "enqueued")
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD, "new"), VEX_SAME_ROUND)
        self.assertIsNone(st.shadow_findings_get(7, ts.HEAD, "old"))
        self.assertEqual(gate.enqueue_shadow(loop, sup, 7, ts.HEAD, turn_key="old"),
                         "snapshot gone")
        self.assertEqual(sup.get(f"acme/widgets:7:{ts.HEAD}:shadow:old")["state"], "failed")
        with mock.patch("diaktoros.config.by_repo", return_value=loop):
            with self.assertRaisesRegex(ValueError, "findings snapshot"):
                sup.retry(sup.get(f"acme/widgets:7:{ts.HEAD}:shadow:old")["id"])
        self.assertEqual(sup.get(f"acme/widgets:7:{ts.HEAD}:shadow:old")["state"], "failed")

    def test_a_supervisor_on_another_ledger_than_the_production_one_is_not_rearmed(self):
        # Review of #1900, F2: the row is looked up in the ledger the supervisor submits to.
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        sup = self.queued_failed_shadow(loop, production=False)
        st.findings_put(7, VEX_SAME_ROUND)
        self.assertEqual(gate.enqueue_shadow(loop, sup, 7, ts.HEAD), "snapshot gone")
        self.assertEqual(sup.get(f"acme/widgets:7:{ts.HEAD}:shadow")["state"], "failed")
        self.assertIsNone(st.shadow_findings_get(7, ts.HEAD))

    def test_an_existing_shadow_whose_snapshot_is_gone_is_not_rearmed_or_given_a_new_one(self):
        # #1139: the row's snapshot was evicted. A second enqueue must not take a fresh one from
        # the live findings ledger and re-arm the row on it (the isolation loss #1013 fixed).
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        sup = self.queued_failed_shadow(loop)
        st.findings_put(7, VEX_SAME_ROUND)             # what the real review recorded since
        self.assertEqual(gate.enqueue_shadow(loop, sup, 7, ts.HEAD), "snapshot gone")
        self.assertEqual(sup.get(f"acme/widgets:7:{ts.HEAD}:shadow")["state"], "failed")
        self.assertIsNone(st.shadow_findings_get(7, ts.HEAD))       # no fresh snapshot

    def test_an_existing_shadow_whose_snapshot_is_kept_is_still_rearmed(self):
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        sup = self.queued_failed_shadow(loop)
        st.shadow_findings_put(7, ts.HEAD, {}, set())
        self.assertEqual(gate.enqueue_shadow(loop, sup, 7, ts.HEAD), "rearmed")
        self.assertEqual(sup.get(f"acme/widgets:7:{ts.HEAD}:shadow")["state"], "pending")

    def test_a_new_reviewer_turn_at_the_head_still_gets_a_snapshot_for_its_own_shadow(self):
        # The refusal is for the row that exists: another turn key at the same head is a new
        # row (a retarget's fresh review), taken on a fresh snapshot as before.
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        sup = self.queued_failed_shadow(loop, turn_key="old")
        self.assertEqual(gate.enqueue_shadow(loop, sup, 7, ts.HEAD, turn_key="new"), "enqueued")
        self.assertEqual(state_mod.state_for(loop).shadow_findings_get(7, ts.HEAD, "new"), {})

    def test_a_snapshot_from_before_turns_were_keyed_is_not_taken_for_any_turns(self):
        # Review of #1900, round 2 F1: a `PR:head` snapshot (written before turns were keyed) has
        # no turn on record, so it must not stand in for an evicted one: the old turn's failed
        # row is neither re-armed on it by the gate nor by `retry`, and the broker ignores it.
        import json
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        sup = self.queued_failed_shadow(loop, turn_key="old")
        st._save_strict(st.dir / "shadow-findings.json",
                        {f"7:{ts.HEAD}": {"entry": {}, "at": time.time()}})
        self.assertIsNone(st.shadow_findings_get(7, ts.HEAD, "old"))
        st.findings_put(7, VEX_SAME_ROUND)
        self.assertEqual(gate.enqueue_shadow(loop, sup, 7, ts.HEAD, turn_key="old"),
                         "snapshot gone")
        old = sup.get(f"acme/widgets:7:{ts.HEAD}:shadow:old")
        self.assertEqual(old["state"], "failed")
        with mock.patch("diaktoros.config.by_repo", return_value=loop):
            with self.assertRaisesRegex(ValueError, "findings snapshot"):
                sup.retry(old["id"])
        self.assertEqual(sup.get(f"acme/widgets:7:{ts.HEAD}:shadow:old")["state"], "failed")
        self.assertEqual(set(json.loads((st.dir / "shadow-findings.json").read_text())),
                         {f"7:{ts.HEAD}"})                      # nothing written beside it
        # The broker of that turn does not read it either: with no snapshot of its own the
        # shadow's verdict is refused (the legacy {} snapshot would have passed it).
        self.scope = dataclasses.replace(self.scope, turn_key="old", shadow=True)
        answer, _ = Snapshot.approve(self)
        self.assertEqual(answer.get("error"), broker_ipc.SHADOW_SNAPSHOT_REFUSAL)

    def test_a_running_shadow_with_only_a_legacy_snapshot_is_not_judged_on_the_live_ledger(self):
        # Adjudication of #1900 (REJECT, F4): a turnless shadow queued before the upgrade, still
        # running, its `PR:head` snapshot holding an open F1 that the live ledger has since lost.
        # Judged on the live ledger its approval would pass; its broker refuses the verdict.
        st = state_mod.state_for(self.fx.loop)
        st._save_strict(st.dir / "shadow-findings.json",
                        {f"7:{ts.HEAD}": {"entry": VEX_SAME_ROUND, "at": time.time()}})
        st.findings_put(7, {})
        self.scope = dataclasses.replace(self.scope, shadow=True)
        answer, server = Snapshot.approve(self)
        self.assertEqual(answer, {"ok": False, "error": broker_ipc.SHADOW_SNAPSHOT_REFUSAL})
        self.assertEqual(server.recorded, [])
        st.shadow_findings_put(7, ts.HEAD, VEX_SAME_ROUND, set())     # its own, F1 open
        answer, _ = Snapshot.approve(self)
        self.assertIn("cannot APPROVE with open finding(s) F1", answer.get("error", ""))

    def test_a_requeued_reviewer_turn_takes_no_snapshot_for_its_shadow_whose_one_is_gone(self):
        # Adjudication of #1900 (REJECT, F1): the reviewer's enqueue took its snapshot before it
        # knew whether its row was new, so a re-armed reviewer turn wrote the evicted shadow row's
        # snapshot from the live ledger, and the gate and `retry` then re-armed that row on it.
        import sqlite3
        from contextlib import closing
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        sup = self.queued_failed_shadow(loop, turn_key="t")
        reviewer = f"acme/widgets:7:{ts.HEAD}:reviewer:t"
        sup.enqueue(reviewer, "acme/widgets", 7, ts.HEAD, "reviewer", turn_key="t")
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET state='failed', error='x' WHERE delivery=?", (reviewer,))
        st.findings_put(7, VEX_SAME_ROUND)                # live findings, since the shadow's turn
        sup.recover = lambda: None
        with mock.patch.object(gate, "isolated_supervisor", return_value=sup):
            self.assertEqual(gate.enqueue_isolated(loop, "reviewer", 7, ts.HEAD, turn_key="t"),
                             "rearmed")
        self.assertIsNone(st.shadow_findings_get(7, ts.HEAD, "t"))      # none taken for it
        self.assertEqual(gate.enqueue_shadow(loop, sup, 7, ts.HEAD, turn_key="t"),
                         "snapshot gone")
        shadow = sup.get(f"acme/widgets:7:{ts.HEAD}:shadow:t")
        with mock.patch("diaktoros.config.by_repo", return_value=loop):
            with self.assertRaisesRegex(ValueError, "findings snapshot"):
                sup.retry(shadow["id"])
        self.assertEqual(sup.get(f"acme/widgets:7:{ts.HEAD}:shadow:t")["state"], "failed")

    def test_a_requeued_reviewer_turn_whose_shadow_kept_its_snapshot_keeps_it(self):
        # The refusal is only for a gone snapshot: one still kept stands, first snapshot wins.
        import sqlite3
        from contextlib import closing
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        sup = self.queued_failed_shadow(loop, turn_key="t")
        st.shadow_findings_put(7, ts.HEAD, {}, set(), "t")
        reviewer = f"acme/widgets:7:{ts.HEAD}:reviewer:t"
        sup.enqueue(reviewer, "acme/widgets", 7, ts.HEAD, "reviewer", turn_key="t")
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET state='failed', error='x' WHERE delivery=?", (reviewer,))
        st.findings_put(7, VEX_SAME_ROUND)
        sup.recover = lambda: None
        with mock.patch.object(gate, "isolated_supervisor", return_value=sup):
            self.assertEqual(gate.enqueue_isolated(loop, "reviewer", 7, ts.HEAD, turn_key="t"),
                             "rearmed")
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD, "t"), {})
        self.assertEqual(gate.enqueue_shadow(loop, sup, 7, ts.HEAD, turn_key="t"), "rearmed")

    def test_a_new_turn_at_a_head_whose_earlier_snapshot_is_kept_gets_its_own(self):
        # Review of #1900, round 2 F3: the put used to return early for the existing PR and head,
        # so the new turn was queued with no snapshot of its own, `retry` refused it as "gone",
        # and the broker judged it on the earlier turn's findings.
        import sqlite3
        from contextlib import closing
        loop = {**self.fx.loop, "shadow": {"profile": "shadow", "every": 1}}
        st = state_mod.state_for(loop)
        sup = self.queued_failed_shadow(loop, turn_key="old")
        st.shadow_findings_put(7, ts.HEAD, VEX_SAME_ROUND, set(), "old")   # kept; F1 open then
        st.findings_put(7, {})                                    # resolved since: live is empty
        self.assertEqual(gate.enqueue_shadow(loop, sup, 7, ts.HEAD, turn_key="new"), "enqueued")
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD, "new"), {})            # its own
        self.assertEqual(st.shadow_findings_get(7, ts.HEAD, "old"), VEX_SAME_ROUND)   # unchanged
        new = sup.get(f"acme/widgets:7:{ts.HEAD}:shadow:new")
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET state='failed', error='x' WHERE id=?", (new["id"],))
        with mock.patch("diaktoros.config.by_repo", return_value=loop):
            self.assertEqual(sup.retry(new["id"]), "pending")       # not refused as "gone"
        # The new turn's broker judges it on its own snapshot: the old turn's F1 is not live.
        self.scope = dataclasses.replace(self.scope, turn_key="new")
        st.findings_put(7, VEX_SAME_ROUND)
        answer, server = Snapshot.approve(self)
        self.assertTrue(answer.get("ok"), answer)
        self.assertEqual(server.recorded[0]["verdict"], "APPROVE")


if __name__ == "__main__":
    unittest.main()
