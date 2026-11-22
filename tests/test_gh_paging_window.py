"""Review on #2578, F1 (again): a later page's window bounds everything the page waits on, not
only its HTTP call. Capping the call's timeout left an App token's mint, signing and the wait for
another minter's lock on the whole budget's deadline, so a page stuck there ran past its window,
spent the rest of the run's share and raised ``GateBudgetExceeded`` out of the listing.

Driven through the real ``gh.fetch`` → ``gh.request`` → ``gh.token`` → ``github_app`` →
``app_tokens`` lock wait, with another minter holding the App token's lock (``test_app_tokens``'
fixture: only the App's own GitHub calls are mocked).
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_tokens as tat  # noqa: E402
from diaktoros import app_tokens, gh, gh_paging  # noqa: E402

PATH = f"/repos/{tat.REPO}/issues?state=open&per_page=100&page=2"


class SlowToken(tat.Fixture):
    hold_lock = tat.Shared.hold_lock         # another minter that never lets go
    def read_page(self, budget: float, per_call: float):
        """``later_page`` on page 2 while the token's lock is held: what it gave, the seconds it
        took, what was left of the run's budget after it, and the run's deadline before/after."""
        self.hold_lock(tat.KEY)
        with gh.gate_scope(time.monotonic() + budget, per_call=per_call) as scope, \
                mock.patch.object(app_tokens, "LOCK_WAIT_S", 60):
            before, start = scope["deadline"], time.monotonic()
            got = gh_paging.later_page(lambda: gh.fetch(self.loop, PATH), "open issue", 2)
            spent, left = time.monotonic() - start, gh.remaining()
        return got, spent, left, (before, scope["deadline"])

    def test_a_page_waiting_on_its_token_stops_at_its_window_and_keeps_the_page_before(self):
        # 4 s budget, 1 s calls: 1 s held back for the rest, the page's window is 1 s.
        got, spent, left, deadlines = self.read_page(4.0, 1.0)
        self.assertEqual(got[:2], (None, None))
        self.assertIn("open issue page 2 not read within the 1s", got[2])
        self.assertIn("waiting for an App token", got[2])
        self.assertLess(spent, 2.0)                     # not the 4 s of the whole budget
        self.assertGreater(left, 1.5)                   # the run keeps its share
        self.assertEqual(deadlines[0], deadlines[1])    # the run's own deadline is back


if __name__ == "__main__":
    unittest.main()
