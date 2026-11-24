"""A receipt barrier write cut inside its run id, end to end (#2643; #2634 review F8).

The redelivery's barrier add writes ``+`` and a few bytes of the reviewer's id, then makes no
progress; the COMMIT fails, and so does the stop. The cut add holds every id it begins, so the
running reviewer's receipt is still refused. A cut that the same redelivery can end whole is
lifted once its shadow row is in, so it holds nothing after: also when the write fails, rather
than making no progress, and when the lift that ends it fails before it lands (#2644 review F2,
F3). A lift that never lands leaves the cut to fail closed, with no record of its offset.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import errno
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_shadow_owed_race as race  # noqa: E402
import test_shadow_owed_stops as stops  # noqa: E402
from diaktoros import review_receipt  # noqa: E402
from diaktoros.run_supervisor import shadow_owing  # noqa: E402
from diaktoros.state import ledger  # noqa: E402
from test_shadow_rearm import REVIEWER  # noqa: E402


class ABarrierCutInsideTheId(race._Race):
    failing_commit = race.AShadowRowThatCannotBeWritten.failing_commit
    refuse_stops = stops.AStopThatCannotBeWritten.refuse_stops
    claim = stops.AStopThatCannotBeWritten.claim

    def cut_after(self, size, times=None, error=None):
        """``pwrite`` writes the first ``size`` bytes, then nothing (or raises ``error``): for
        every write after, or up to write ``times``, so the add raises and a later write goes
        through."""
        real, cuts = ledger.os.pwrite, [0]

        def pwrite(fd, data, at):
            cuts[0] += 1
            if cuts[0] == 1:
                return real(fd, data[:size], at)
            if times is not None and cuts[0] > times:
                return real(fd, data, at)
            if error is not None:
                raise error
            return 0
        return mock.patch.object(ledger.os, "pwrite", pwrite)

    def holds_another(self):
        """Whether the barrier refuses a turn with no shadow row, and what it holds."""
        with ledger.claim_lock(self.sup().db) as held, \
                contextlib.closing(sqlite3.connect(self.sup().db)) as con:
            return ledger.owes(held, con, "a-turn-with-no-shadow-row"), ledger._owing(held)

    def lock_path(self):
        db = pathlib.Path(self.sup().db)
        return db.with_name(db.name + ".claims.lock")

    def lock_bytes(self):
        return self.lock_path().read_bytes()

    def test_a_cut_inside_the_id_with_every_write_after_failing_still_refuses(self):
        # The adjudicator's and Vex's round-9 probe: 5 bytes, then nothing.
        loop = self.queued_unsampled()
        self.refuse_stops()
        with self.race("running"), self.failing_commit(), self.cut_after(5), \
                self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
            self.enqueue(loop)
        run_id = self.sup().get(REVIEWER)["id"]
        self.assertEqual(self.lock_bytes(), b"+" + run_id[:4].encode())
        self.assertEqual(self.rows(), [("reviewer", "running")])
        with self.assertRaisesRegex(review_receipt.ReceiptDenied, "owes a shadow"):
            self.claim()
        self.assertEqual(self.enqueue(loop), "duplicate running")    # control: the shadow row
        self.claim()

    def test_a_cut_its_redelivery_ends_whole_holds_nothing_after(self):
        # The add is cut and raises; the redelivery commits its shadow row, then ends it whole.
        loop = self.queued_unsampled()
        with self.race("running"), self.cut_after(5, times=2):
            self.assertEqual(self.enqueue(loop), "duplicate running")
        self.assertEqual(self.lock_bytes(), b"")
        self.claim()

    def test_another_writers_cut_outlives_a_redelivery_whose_add_wrote_nothing(self):
        # #2644 review F1: a writer died after "+"; this redelivery's add writes nothing, then
        # commits its shadow row and clears. The dead writer's cut add still holds, so a turn
        # with no shadow row is refused.
        loop = self.queued_unsampled()
        self.lock_path().write_bytes(b"+")
        with self.race("running"), self.cut_after(0, times=1):
            self.assertEqual(self.enqueue(loop), "duplicate running")
        self.assertEqual(self.holds_another(), (True, (set(), [""])))
        self.claim()                            # this turn's own shadow row is in

    def test_a_cut_whose_write_failed_is_lifted_by_its_writer(self):
        # #2644 review F3: "+" lands, then the write raises ENOSPC rather than writing nothing.
        loop = self.queued_unsampled()
        with self.race("running"), self.cut_after(1, times=2, error=OSError(errno.ENOSPC, "full")):
            self.assertEqual(self.enqueue(loop), "duplicate running")
        self.assertEqual(self.lock_bytes(), b"")
        self.assertEqual(self.holds_another(), (False, (set(), [])))
        self.claim()

    def test_a_lift_that_fails_is_tried_again_and_lifts_the_cut(self):
        # #2644 review F2: "+", no progress, the shadow row commits, then the lift that ends the
        # redelivery makes no progress once. It is tried again, with the cut it owns.
        loop = self.queued_unsampled()
        with self.race("running"), self.cut_after(1, times=3):
            self.assertEqual(self.enqueue(loop), "duplicate running")
        self.assertEqual(self.lock_bytes(), b"")
        self.assertEqual(self.holds_another(), (False, (set(), [])))
        self.claim()

    def test_a_lift_that_never_lands_fails_closed_and_names_no_offset(self):
        # #2644 review F4: an offset named anywhere could later be another writer's cut, once the
        # log is emptied, so no record of it is kept: the cut holds as a dead writer's does.
        loop = self.queued_unsampled()
        with self.race("running"), self.cut_after(1), \
                mock.patch.object(shadow_owing, "LIFT_WAITS", (0, 0)):
            self.assertEqual(self.enqueue(loop), "duplicate running")
        self.assertEqual(self.lock_bytes(), b"+")
        self.assertEqual(self.holds_another(), (True, (set(), [""])))
        with contextlib.closing(sqlite3.connect(self.sup().db)) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM ledger_events").fetchone(), (0,))
        self.claim()                            # this turn's own shadow row is in

if __name__ == "__main__":
    unittest.main()
