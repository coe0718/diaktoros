"""#2635: the poller-lock test (``test_call_volume.ThirtyHeldPRsSeatFree``) compares its locked
and unlocked spans on workers started and on reads, each on its own. Compared as one tuple, the
way it once was, fewer workers with the lock passed the check whatever the reads came to, so a
lock that cut one worker and multiplied the reads would still pass.

The real spans give the same counts with and without the lock, which both checks pass. So these
cases run that very test method with its spans replaced by counts that tell the two checks apart:
put the tuple check back in it and the first case passes, and fails here."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import unittest
from unittest import mock

import test_call_volume

NAME = 'test_the_poller_lock_cuts_lingerers_with_no_more_calls_and_the_same_decisions'
UNLOCKED = (99, 294, 98)        # started, reads, peak lingering: the real unlocked span's counts


class TheThirtyPRCheck(unittest.TestCase):
    def run_it(self, locked):
        """Run the 30-PR test with these counts for its locked span; its own assertions decide."""
        spans = {False: UNLOCKED, True: locked}
        thirty = test_call_volume.ThirtyHeldPRsSeatFree    # not a module name: loaded once
        test = thirty(NAME)
        with mock.patch.object(thirty, 'span', lambda _self, locked: spans[locked]), \
                contextlib.redirect_stdout(io.StringIO()):
            getattr(test, NAME)()

    def test_fewer_workers_do_not_hide_more_reads(self):
        with self.assertRaisesRegex(AssertionError, '9999 not less than or equal to 294'):
            self.run_it((98, 9999, 1))

    def test_fewer_reads_do_not_hide_more_workers(self):
        with self.assertRaisesRegex(AssertionError, '100 not less than or equal to 99'):
            self.run_it((100, 10, 1))

    def test_no_more_of_either_passes(self):
        self.run_it((98, 294, 1))
        self.run_it((99, 294, 1))       # the real spans: equal counts


if __name__ == '__main__':
    unittest.main()
