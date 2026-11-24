"""#2635: the poller-lock test (test_call_volume.ThirtyHeldPRsSeatFree) compares its locked and
unlocked spans on workers started and on reads, each on its own. Compared as one tuple, the way it
once was, fewer workers with the lock passed the check whatever the reads came to, so a lock that
cut one worker and multiplied the reads would still pass.

The real spans give the same counts with and without the lock, which both checks pass; these
cases pin the component-wise check with counts that tell the two apart."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest


def assert_no_more_calls(test, locked, unlocked):
    """Fail ``test`` unless the locked span (``(started, reads, ...)``) started no more workers
    *and* made no more reads than the unlocked one, each compared on its own (#2635)."""
    test.assertLessEqual(locked[0], unlocked[0], 'more workers started with the lock')
    test.assertLessEqual(locked[1], unlocked[1], 'more reads with the lock')


UNLOCKED = (99, 294, 98)        # started, reads, peak lingering: the real unlocked span's counts


class NoMoreCalls(unittest.TestCase):
    def test_fewer_workers_do_not_hide_more_reads(self):
        with self.assertRaisesRegex(AssertionError, 'more reads with the lock'):
            assert_no_more_calls(self, (98, 9999, 1), UNLOCKED)

    def test_fewer_reads_do_not_hide_more_workers(self):
        with self.assertRaisesRegex(AssertionError, 'more workers started with the lock'):
            assert_no_more_calls(self, (100, 10, 1), UNLOCKED)

    def test_no_more_of_either_passes(self):
        assert_no_more_calls(self, (98, 294, 1), UNLOCKED)
        assert_no_more_calls(self, (99, 294, 1), UNLOCKED)    # the real spans: equal counts


if __name__ == '__main__':
    unittest.main()
