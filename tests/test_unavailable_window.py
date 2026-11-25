"""#2177: the outage signature is searched in the window ``runs.detail`` keeps, no wider.

The capture hands ``turn.observed`` ``DETAIL_MARGIN`` times the stored size of each stream (#2083)
only so redaction sees a token at the cut whole. ``_account_after_turn`` slices each stream back to
its last ``DETAIL_STDOUT_BYTES`` / ``DETAIL_STDERR_BYTES`` before ``pacing.unavailable_signature``:
an outage line older than that is history, and holding the account on it would hold every seat on
it, for a growing window, where the old capture never saw it.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import pacing, run_supervisor  # noqa: E402
import test_pacing as tp  # noqa: E402

SIGN = "Provider returned an empty stream with no finish_reason"


def failing(stdout="", stderr=""):
    """A fake turn that exits 1 with these streams observed, as the margin capture hands them."""
    def run(_loop, scope, observed=None, **kw):
        observed.update(stdout=stdout, stderr=stderr)
        return 1
    return run


class SignatureWindow(tp.ProductionBase):
    def held_by(self, **streams):
        row = self.run_production(failing(**streams))
        return row, pacing.held(self.account)

    def test_a_sign_before_the_stdout_window_does_not_hold_the_account(self):
        filler = "x" * run_supervisor.DETAIL_STDOUT_BYTES
        row, hold = self.held_by(stdout=SIGN + "\n" + filler)
        self.assertIsNone(hold, "an outage line outside the kept window held the account")
        self.assertEqual((row["state"], row["retries"]), ("waiting", 1))

    def test_a_sign_inside_the_stdout_window_holds_it(self):
        filler = "x" * (run_supervisor.DETAIL_STDOUT_BYTES - len(SIGN) - 1)
        row, hold = self.held_by(stdout=filler + "\n" + SIGN)
        self.assertIsNotNone(hold)
        self.assertEqual((row["state"], row["retries"]), ("waiting", 0))

    def test_a_sign_before_the_stderr_window_does_not_hold_the_account(self):
        filler = "x" * run_supervisor.DETAIL_STDERR_BYTES
        self.assertIsNone(self.held_by(stderr=SIGN + "\n" + filler)[1])

    def test_a_sign_inside_the_stderr_window_holds_it(self):
        filler = "x" * (run_supervisor.DETAIL_STDERR_BYTES - len(SIGN) - 1)
        self.assertIsNotNone(self.held_by(stderr=filler + "\n" + SIGN)[1])


if __name__ == "__main__":
    unittest.main()
