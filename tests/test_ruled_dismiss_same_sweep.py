"""#2619: the dismissal of the overruled change request follows the ruled approval in one sweep.

Before, ``ruled_approval._act`` took one step per PR per sweep: the approval, then, a whole sweep
(up to 15 minutes) later, the dismissal, and the merge check stayed red in between. Now a posted
approval that reads back from GitHub is followed in the same sweep by the dismissal, under every
check it always had; an approval not read back yet, or left uncertain or denied, still leaves the
dismissal to a later sweep. GitHub, the CI read and the broker's writes are mocked, as in
test_ruled_approval_sweep; here an approval reads back at once unless a test says otherwise.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ruled_approval as tra  # noqa: E402
import test_ruled_approval_sweep as ras  # noqa: E402
from diaktoros import broker, merge_check, ruled_approval  # noqa: E402

CHANGES, TUCK = tra.CHANGES, tra.TUCK


class SameSweep(ras.Base):
    kinds = ras.CheckShowsIt.kinds

    def setUp(self):
        super().setUp()
        self.mark_escalated(by="dispute")
        self.ruled("ACCEPT")
        self.green_at_refresh = []
        merge_check.refresh_quietly.side_effect = lambda *_a: self.green_at_refresh.append(
            merge_check.decide(self.kinds())[0] == merge_check.SUCCESS)

    def _approved(self, *_a, **kw):                  # read back by the very next read
        self.reviews = [*self.reviews, {**TUCK, "body": kw.get("text", "")}]
        return TUCK["id"]

    def steps(self):
        row = self.row()
        return (row["approve"], row["dismiss"]) if row else None

    def test_one_sweep_approves_then_dismisses_and_the_check_goes_green(self):
        self.assertEqual(self.kinds()[0][0], ras.ready_mod().FAILURE)
        self.assertEqual(self.sweep(), [7])
        self.approve.assert_called_once()
        self.dismiss.assert_called_once()
        self.assertEqual(self.dismiss.call_args.kwargs["review_id"], CHANGES["id"])
        self.assertEqual(self.steps(), ("posted", "posted"))
        self.assertEqual(self.green_at_refresh, [True])          # one refresh, after both
        self.assertEqual(self.sweep(), [])                       # nothing is written again
        self.approve.assert_called_once()
        self.dismiss.assert_called_once()

    def test_an_approval_not_read_back_yet_leaves_the_dismissal_to_the_next_sweep(self):
        self.approve.side_effect = lambda *a, **kw: ras.Base._approved(self, *a, **kw)
        self.assertEqual(self.sweep(), [7])
        self.dismiss.assert_not_called()
        self.assertEqual(self.steps(), ("posted", "pending"))   # waits; never denied for lag
        self.assertEqual(self.green_at_refresh, [False])
        self.assertEqual(self.sweep(), [7])                      # read back now: dismissed
        self.dismiss.assert_called_once()
        self.assertEqual(self.steps(), ("posted", "posted"))
        self.assertEqual(self.green_at_refresh, [False, True])

    def test_a_newer_verdict_between_the_two_steps_stops_the_dismissal(self):
        def approved_then_verdict(*a, **kw):
            review_id = self._approved(*a, **kw)
            self.reviews.append(tra.review(29, "CHANGES_REQUESTED", at="2026-01-04T00:00:00Z"))
            return review_id
        self.approve.side_effect = approved_then_verdict
        self.sweep()
        self.dismiss.assert_not_called()
        self.assertEqual(self.steps(), ("posted", "denied"))
        self.assertIn("a newer verdict at the head", self.row()["error"])

    def test_an_uncertain_approval_never_goes_on_to_the_dismissal(self):
        def landed(*a, **kw):
            self._approved(*a, **kw)          # it landed, but the answer was lost
            raise TimeoutError("read timed out")
        self.approve.side_effect = landed
        self.sweep()
        self.dismiss.assert_not_called()
        self.assertEqual(self.steps(), ("uncertain", "pending"))  # read back only next sweep
        self.sweep()
        self.assertEqual(self.steps(), ("posted", "pending"))
        self.dismiss.assert_not_called()

    def test_a_denied_approval_never_goes_on_to_the_dismissal(self):
        with mock.patch.object(broker, "authorize_adjudicator",
                               side_effect=broker.BrokerDenied("stale PR head")):
            self.sweep()
        self.approve.assert_not_called()
        self.dismiss.assert_not_called()
        self.assertEqual(self.steps(), ("denied", "pending"))

    def test_a_hold_that_lands_after_the_approval_stops_the_dismissal(self):
        # The dismissal's own checks run again: a maintainer hold read after the approval.
        holds = iter([(None, ""), ({"by": "coe0718"}, "")])
        with mock.patch("diaktoros.pr_hold.read", side_effect=lambda *_a: next(holds)):
            self.sweep()
        self.approve.assert_called_once()
        self.dismiss.assert_not_called()
        self.assertEqual(self.steps(), ("posted", "pending"))

    def test_a_failed_dismissal_still_refreshes_the_posted_approval(self):
        # The approval posted; an error in the opportunistic dismissal must not cost its refresh.
        real, calls = ruled_approval._wait_or_deny, []
        def second_fails(*a, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise sqlite3.OperationalError("database is locked")
            return real(*a, **kw)
        with mock.patch.object(ruled_approval, "_wait_or_deny", side_effect=second_fails):
            self.assertEqual(self.sweep(), [7])
        self.assertEqual(self.steps(), ("posted", "pending"))   # the next sweep dismisses
        self.assertEqual(self.green_at_refresh, [False])          # but the check is refreshed
        self.assertEqual(self.sweep(), [7])
        self.assertEqual(self.steps(), ("posted", "posted"))


if __name__ == "__main__":
    unittest.main()
