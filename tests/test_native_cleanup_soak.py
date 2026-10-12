"""Acceptance accounting is strict even when eventual cleanup succeeds late."""
import _home_guard  # noqa: F401
import unittest

from native_cleanup_soak import AcceptanceError, PendingBudget, require_exits


class SoakAcceptanceTests(unittest.TestCase):
    def test_image_must_disappear_within_five_total_sweeps(self):
        budget=PendingBudget()
        budget.add('/fixture',0)
        for tick in range(4):
            budget.begin_sweep(tick)
            budget.complete_sweep(tick,lambda _:True)
        budget.begin_sweep(4)
        with self.assertRaises(AcceptanceError):
            budget.complete_sweep(4,lambda _:True)

    def test_late_disappearance_cannot_pass_time_deadline(self):
        budget=PendingBudget(); budget.add('/fixture',0)
        budget.begin_sweep(1)
        with self.assertRaises(AcceptanceError):
            budget.complete_sweep(301,lambda _:False)

    def test_success_records_latency_and_clears_only_absent_images(self):
        budget=PendingBudget(); budget.add('/a',0); budget.add('/b',0)
        budget.begin_sweep(1)
        budget.complete_sweep(2,lambda p:p=='/b')
        self.assertEqual(set(budget.pending),{'/b'})
        budget.begin_sweep(3); budget.complete_sweep(4,lambda _:False)
        self.assertEqual(budget.pending,{})
        self.assertEqual(budget.worst_sweeps,2)
        self.assertEqual(budget.worst_seconds,4)

    def test_empty_group_does_not_replace_registered_exit_events(self):
        with self.assertRaises(AcceptanceError): require_exits(set(),set(),set())
        with self.assertRaises(AcceptanceError): require_exits({10,11},{10},set())
        with self.assertRaises(AcceptanceError): require_exits({10},{10},{11})
        require_exits({10,11},{10,11},set())
