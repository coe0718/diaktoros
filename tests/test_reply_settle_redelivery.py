"""#2248 review: a reply's redelivery keeps the settle window and its worker.

F2: a settling row whose worker never spawned was left waiting for an unrelated ``recover`` to
start it once due; a redelivery found it ``duplicate waiting`` and spawned nothing, while the
reply wake and the ask resume took the turn as queued. Now a redelivery of a settling row spawns
a worker (it lingers until the row is due) and keeps the row's own deadline.

F3: a redelivery that re-armed a failed reply row made it pending at once, so a retried reply
skipped the window. Now it waits on ``SETTLE_HOLD`` as a new row would.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_issue_fix as base  # noqa: E402
import test_reply_settle as settle  # noqa: E402
BASE, REPO, row = base.BASE, base.REPO, settle.row

from diaktoros import run_supervisor  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402
from diaktoros.state import ledger  # noqa: E402


def load_tests(loader, standard_tests, pattern):
    """This file's own tests only (``base.Base``'s setUp is borrowed, not its tests)."""
    suite = unittest.TestSuite()
    for case in (LostSpawn, RearmedReply):
        names = sorted(n for n in vars(case) if n.startswith("test_"))
        suite.addTests(case(name) for name in names)
    return suite


class Redelivery(base.Base):
    def setUp(self):
        super().setUp()
        self.sup = Supervisor(self.db, fixture_mode=True, fixture_command=["true"])
        patch = mock.patch.object(Supervisor, "_spawn")
        self.spawn = patch.start()
        self.addCleanup(patch.stop)

    def submit(self, wait=120, delivery="d") -> str:
        return self.sup.submit(delivery, REPO, 12, BASE, "issue_fixer",
                               turn_key="issue-fix:reply1", settle=wait)


class LostSpawn(Redelivery):
    def test_a_redelivery_of_a_settling_row_spawns_its_worker_and_keeps_its_deadline(self):
        self.spawn.side_effect = OSError("spawn failed")
        with self.assertRaises(OSError):
            self.submit()
        first = row(self.db)
        self.assertEqual((first["state"], first["error"]), ("waiting", run_supervisor.SETTLE_HOLD))
        self.spawn.reset_mock(side_effect=True)
        outcome = self.submit()
        self.assertTrue(outcome.startswith("duplicate waiting"), outcome)
        self.assertEqual(self.spawn.call_count, 1)          # a worker lingers until it is due
        self.assertEqual(row(self.db), first)               # the original deadline, not a new one

    def test_a_redelivery_of_a_backoff_wait_still_spawns_nothing(self):
        self.submit(wait=0)
        with ledger.connect(self.db) as con:                # an ordinary pre-write retry backoff
            con.execute("UPDATE runs SET state='waiting', retry_at=?, error='claim read failed'",
                        (time.time() + 60,))
        self.spawn.reset_mock()
        self.assertTrue(self.submit(wait=0).startswith("duplicate waiting"))
        self.assertEqual(self.spawn.call_count, 0)


class RearmedReply(Redelivery):
    def fail(self):
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', retry_at=NULL, "
                        "error='worker exited before launch'")

    def test_a_rearmed_reply_row_waits_out_the_window(self):
        self.submit()
        self.fail()
        now = time.time()
        self.assertEqual(self.submit(), "rearmed")
        held = row(self.db)
        self.assertEqual((held["state"], held["error"]), ("waiting", run_supervisor.SETTLE_HOLD))
        self.assertAlmostEqual(held["retry_at"], now + 120, delta=5)
        self.assertIsNone(self.sup._claim())                # not runnable now
        settle.fall_due(self.db)
        self.sup.recover()
        self.assertIsNotNone(self.sup._claim())             # and runnable once due

    def test_with_no_window_a_rearm_is_pending_at_once_as_before(self):
        self.submit(wait=0)
        self.fail()
        self.assertEqual(self.submit(wait=0), "rearmed")
        self.assertEqual((row(self.db)["state"], row(self.db)["retry_at"]), ("pending", None))


if __name__ == "__main__":
    unittest.main()
