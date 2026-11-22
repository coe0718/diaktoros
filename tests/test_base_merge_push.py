"""``update_behind`` at the push (#1785): what is read and written between the merge and the push.

#2288: the head's reviews are read again right before the push, so a change request landing
while the merge is built stops it. #2289: the carried-approval record is written before the push,
and a record that cannot be written stops the push. #2292: ``ready``'s own reason for a behind
head is the one ``base_merge`` tells apart, read through the real ``ready.assess_kinds``.

Built on ``test_base_merge.Base``: real Git, a local bare remote, nothing on the network.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import time
import unittest
from unittest import mock

import test_base_merge as tbm
from diaktoros import base_merge, ready, safe_push, state as state_mod


def during_merge(change):
    """``change`` runs while the merge is built: after the sweep's read, before the push."""
    real = safe_push._dry

    def dry(*args, **kwargs):
        change()
        return real(*args, **kwargs)
    return mock.patch.object(safe_push, "_dry", side_effect=dry)


class NotPushed:
    def assert_not_pushed(self):
        self.assertEqual(self.remote_head(), self.head)
        self.assertEqual(base_merge._records(self.loop), {})
        self.assertEqual(self.notices, [])


class ReadAgain(NotPushed, tbm.Base):
    """#2288: the reviews at the sweep's read were idle; something lands while the merge runs."""

    during_merge = staticmethod(during_merge)

    def test_a_change_request_posted_while_the_merge_runs_stops_the_push(self):
        late = tbm.approval(self.head, review_id=12, state="CHANGES_REQUESTED",
                            at="2026-11-10T02:00:00Z")
        with self.during_merge(lambda: self.reviews.append(late)), \
                mock.patch.object(base_merge, "log") as log:
            self.assertEqual(self.update(), "")
        self.assert_not_pushed()
        self.assertIn("changes are requested at the head, read again before the push",
                      str(log.call_args_list[-1]))

    def test_reviews_unreadable_at_the_second_read_stop_the_push(self):
        with self.during_merge(lambda: setattr(self, "reviews", None)):
            self.assertEqual(self.update(), "")
        self.assert_not_pushed()

    def test_an_undated_change_request_posted_while_the_merge_runs_stops_the_push(self):
        # Review on #2576, F1: an undated verdict leaves no provable latest one; it may be a
        # change request still owed, so the push waits rather than reading it as none.
        late = tbm.approval(self.head, review_id=12, state="CHANGES_REQUESTED", at=None)
        with self.during_merge(lambda: self.reviews.append(late)), \
                mock.patch.object(base_merge, "log") as log:
            self.assertEqual(self.update(), "")
        self.assert_not_pushed()
        self.assertIn("verdicts at the head cannot be ordered, read again before the push",
                      str(log.call_args_list[-1]))

    def test_an_undated_verdict_at_the_sweeps_read_waits(self):
        self.reviews.append(tbm.approval(self.head, review_id=12, state="CHANGES_REQUESTED", at=None))
        with mock.patch.object(base_merge, "log") as log:
            self.assertEqual(self.update(), "")
        self.assert_not_pushed()
        self.assertIn("cannot be ordered", str(log.call_args_list[-1]))

    def test_a_head_with_no_verdict_still_merges(self):
        # Only an indeterminate verdict waits: an unreviewed head is merged in, with nothing carried.
        self.reviews = [tbm.approval(self.head, review_id=12, state="COMMENTED")]
        new = self.update()
        self.assertEqual(new, self.remote_head())
        self.assertNotEqual(new, self.head)
        self.assertIsNone(self.carried(new))

    def test_an_unchanged_review_list_still_pushes_and_carries(self):
        new = self.update()
        self.assertEqual(new, self.remote_head())
        self.assertEqual(self.carried(new)["from"], self.head)


class RecordFirst(tbm.Base):
    """#2289: the carried approval is on disk before the push, and none means no push."""

    def test_the_record_exists_when_the_push_is_attempted(self):
        seen = []
        real = safe_push._audit

        def audit(loop, record):
            if record.get("phase") == "attempt":
                seen.append(self.carried(record["new_head"]))
            return real(loop, record)
        with mock.patch.object(safe_push, "_audit", side_effect=audit):
            new = self.update()
        self.assertEqual(new, self.remote_head())
        self.assertEqual(len(seen), 1)
        self.assertEqual((seen[0] or {}).get("from"), self.head)

    def test_a_record_that_cannot_be_written_pushes_nothing(self):
        with mock.patch.object(state_mod.LoopState, "kv_set_strict",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.update()
        self.assertEqual(self.remote_head(), self.head)
        self.assertEqual(self.notices, [])


class LateRuling(NotPushed, tbm.RuledBase):
    """Review on #2576, F2: settled at the sweep's read, the adjudicator's ACCEPT is acted on again
    while the merge is built; the second read sees its write on the wire and stops the push."""

    def test_a_ruling_write_on_the_wire_by_the_push_stops_it(self):
        for state in ("posting", "uncertain"):
            with self.subTest(state):
                self.settle()
                with during_merge(lambda: self.rows[0].__setitem__("dismiss", state)), \
                        mock.patch.object(base_merge, "log") as log:
                    self.assertEqual(self.update(), "")
                self.assert_not_pushed()
                self.assertIn(f"(dismiss: {state}), read again before the push",
                              str(log.call_args_list[-1]))

    def test_settled_at_both_reads_pushes_and_carries(self):
        self.settle()
        with during_merge(lambda: None):
            new = self.update()
        self.assertEqual(new, self.remote_head())
        self.assertEqual(self.carried(new)["review"], 21)


class HoldLapses(NotPushed, tbm.Base):
    """Review on #2576, F3: the reviews read after the hold is taken outlast ``HOLD_S``, so the
    hold lapses and a turn is claimed at the head; the push checks its hold first and stops."""

    def lapse_after_take(self, db, lapse, claim):
        from diaktoros.run_supervisor import merge_hold
        real = merge_hold.take

        def take(*args, **kwargs):
            got = real(*args, **kwargs)
            with sqlite3.connect(db) as con:
                if lapse:
                    con.execute("UPDATE merge_holds SET created=?",
                                (time.time() - merge_hold.HOLD_S - 1,))
                if claim:
                    now = time.time()
                    con.execute("INSERT INTO runs (id, delivery, repo, pr, head, seat, state, "
                                "created, updated) VALUES ('r', 'd', ?, 7, ?, 'reviewer', "
                                "'running', ?, ?)", (tbm.REPO, self.head, now, now))
            con.close()
            return got
        return mock.patch.object(merge_hold, "take", side_effect=take)

    def test_a_hold_that_lapsed_during_the_reads_stops_the_push(self):
        from diaktoros import run_supervisor
        for lapse, claim, said in ((True, False, "the merge hold lapsed"),
                                   (True, True, "the merge hold lapsed"),
                                   (False, True, "a reviewer turn is running")):
            with self.subTest(lapse=lapse, claim=claim):
                db = self.root / f"runs-{lapse}-{claim}.sqlite"
                run_supervisor.Supervisor(db)
                with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=db), \
                        self.lapse_after_take(db, lapse, claim), \
                        mock.patch.object(base_merge, "log") as log:
                    self.assertEqual(self.update(), "")
                self.assert_not_pushed()
                self.assertIn(said, str(log.call_args_list[-1]))


class LockWait(NotPushed, tbm.Base):
    """Review on #2576, F3 again: ``confirm`` waits for the writer lock (up to
    ``ledger.LOCK_WAIT_S``) and the hold lapses meanwhile, while a reviewer turn is claimed and
    finishes. Its clock is read once the lock is held, so the lapse stops the push."""

    def test_a_hold_that_lapses_while_confirm_waits_for_the_lock_stops_the_push(self):
        import contextlib
        from types import SimpleNamespace

        from diaktoros import run_supervisor
        from diaktoros.run_supervisor import merge_hold
        db, skew, armed = self.root / "runs.sqlite", [0.0], []
        run_supervisor.Supervisor(db)
        real_take, real_connect = merge_hold.take, merge_hold.ledger.connect

        def take(*args, **kwargs):
            got = real_take(*args, **kwargs)
            now = time.time()            # a reviewer turn claimed and finished under the wait
            with contextlib.closing(sqlite3.connect(db)) as con, con:
                con.execute("INSERT INTO runs (id, delivery, repo, pr, head, seat, state, created,"
                            " updated) VALUES ('r', 'd', ?, 7, ?, 'reviewer', 'succeeded', ?, ?)",
                            (tbm.REPO, self.head, now, now))
            armed.append(True)
            return got

        def lapse_at_lock(sql):          # the lock is granted only after the hold has lapsed
            if armed and sql.startswith("BEGIN IMMEDIATE"):
                skew[0] = merge_hold.HOLD_S + 1

        @contextlib.contextmanager
        def connect(*args, **kwargs):
            with real_connect(*args, **kwargs) as con:
                con.set_trace_callback(lapse_at_lock)
                yield con
        clock = SimpleNamespace(time=lambda: time.time() + skew[0])
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=db), \
                mock.patch.object(merge_hold, "take", side_effect=take), \
                mock.patch.object(merge_hold.ledger, "connect", connect), \
                mock.patch.object(merge_hold, "time", clock), \
                mock.patch.object(base_merge, "log") as log:
            self.assertEqual(self.update(), "")
        self.assertEqual(armed, [True])
        self.assert_not_pushed()
        self.assertIn("the merge hold lapsed", str(log.call_args_list[-1]))


class RealReady(tbm.Base):
    """#2292: no mock of ``ready.assess_kinds``: its reason for a behind, approved, green head is
    the one ``base_merge.source`` sets aside, so the approval carries."""

    def assess(self):
        return ready.assess_kinds(self.loop, self.st, self.live, self.reviews, tbm.GREEN, self.live)

    def test_ready_names_only_behind_and_it_is_the_exported_reason(self):
        self.assertEqual([text for _kind, text in self.assess()], [ready.BEHIND])

    def test_source_finds_the_approval_through_the_real_ready(self):
        reviews = self.reviews
        with mock.patch.object(tbm.ci, "read", return_value=tbm.GREEN):
            found, why = base_merge.source(self.loop, self.st, self.head, self.live, reviews, reviews)
        self.assertEqual(why, "")
        self.assertEqual((found or {}).get("id"), 11)


if __name__ == "__main__":
    unittest.main()
