"""A receipt barrier write cut inside its run id, end to end (#2643; #2634 review F8).

The redelivery's barrier add writes ``+`` and a few bytes of the reviewer's id, then makes no
progress; the COMMIT fails, and so does the stop. The cut add holds every id it begins, so the
running reviewer's receipt is still refused. A cut that the same redelivery can end whole is
lifted once its shadow row is in, so it holds nothing after.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_shadow_owed_race as race  # noqa: E402
import test_shadow_owed_stops as stops  # noqa: E402
from diaktoros import review_receipt  # noqa: E402
from diaktoros.state import ledger  # noqa: E402
from test_shadow_rearm import REVIEWER  # noqa: E402


class ABarrierCutInsideTheId(race._Race):
    failing_commit = race.AShadowRowThatCannotBeWritten.failing_commit
    refuse_stops = stops.AStopThatCannotBeWritten.refuse_stops
    claim = stops.AStopThatCannotBeWritten.claim

    def cut_after(self, size, times=None):
        """``pwrite`` writes the first ``size`` bytes, then nothing: for every write after, or
        up to write ``times``, so the add raises and a later write goes through."""
        real, cuts = ledger.os.pwrite, [0]

        def pwrite(fd, data, at):
            cuts[0] += 1
            if cuts[0] == 1:
                return real(fd, data[:size], at)
            return real(fd, data, at) if times is not None and cuts[0] > times else 0
        return mock.patch.object(ledger.os, "pwrite", pwrite)

    def lock_bytes(self):
        db = pathlib.Path(self.sup().db)
        return db.with_name(db.name + ".claims.lock").read_bytes()

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


if __name__ == "__main__":
    unittest.main()
