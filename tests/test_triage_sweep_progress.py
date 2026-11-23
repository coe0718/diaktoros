"""Review on #2578, F7: a resumed sweep read the places around its page's start again before the
page itself (F6), so when those places took most of the budget the page never started, even a
fast one, and the next sweep started over from page 1. With page 1 and the places both slow,
sweeps took turns between them and an eligible issue just past the places was never queued.

Now the page goes first, and the places right after it, before any later page. Driven through
``watchdog.sweep_loop`` with the real listing reads and a clock each read moves on: a 20 s budget,
page 1 taking 18 s, the places 14.5 s and page 2 no time. Places 1-134 are PRs (never queued)
and place 135 is eligible issue #8.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_triage_sweep as tts  # noqa: E402
from diaktoros import gh  # noqa: E402

LISTING = ([tts.issue(900 + n, pr=True) for n in range(134)]
           + [tts.issue(8, age=7200.0)])                # older than every PR before it


class SlowPlaces(tts.SweepBase):

    def run_sweep(self, took: dict):
        """One sweep; ``took`` maps a read (``1``, ``2`` or ``"places"``) to the seconds it takes.
        Returns the sweep's alerts and the reads it made."""
        seen, skew, real = [], [0.0], time.monotonic

        def fetch(loop, path, *args, **kw):
            if "/issues?" not in path:
                return [], None               # the sweep's other reads: nothing to say
            size, at = re.search(r"per_page=(\d+)(?:&page=(\d+))?$", path).groups()
            size, at = int(size), int(at or 1)
            read = at if size == gh.REVIEW_PAGE_SIZE else "places"
            seen.append(read)
            skew[0] += took.get(read, 0.0)
            return LISTING[(at - 1) * size:at * size], None

        with mock.patch("time.monotonic", lambda: real() + skew[0]), \
                gh.gate_scope(time.monotonic() + 20.0, per_call=20.0), \
                mock.patch.object(gh, "fetch", side_effect=fetch):
            lines = self.sweep(None, read=gh.open_issues_read)
        return [line for line in lines if "⚠️" in line and "recovery sweep" in line], seen

    def queued(self):
        return [e[1] for e in self.enqueued]

    def test_slow_places_never_keep_a_fast_page_from_being_read(self):
        took = {1: 18.0, "places": 14.5}
        alerts, seen = self.run_sweep(took)               # page 1, then cut
        self.assertEqual((seen, self.queued()), ([1], []))
        self.assertIn("the next sweep reads on from there", alerts[0])
        alerts, seen = self.run_sweep(took)               # page 2 first, then the places
        self.assertEqual(seen, [2, "places"])
        self.assertEqual((alerts, self.queued()), ([], [8]))    # the issue, and no PR

    def test_it_keeps_moving_over_many_sweeps(self):
        # Vex's probe ran four sweeps and queued nothing; the issue is queued once, on the second.
        took = {1: 18.0, "places": 14.5}
        for _sweep in range(4):
            self.run_sweep(took)
        self.assertEqual(self.queued(), [8])

    def test_places_that_never_fit_after_the_page_still_start_over(self):
        # A page that leaves no room for the places proves nothing: it is checked, and the next
        # sweep starts from page 1, never past an issue that moved before the page.
        took = {1: 18.0, 2: 14.5, "places": 14.5}
        self.run_sweep(took)
        alerts, seen = self.run_sweep(took)
        self.assertEqual(seen, [2])
        self.assertEqual(self.queued(), [8])              # on the page it read: checked
        self.assertIn("places around page 2 not read", alerts[0])
        self.assertIn("the next sweep starts again from page 1", alerts[0])
        alerts, seen = self.run_sweep(took)
        self.assertEqual(seen, [1])


if __name__ == "__main__":
    unittest.main()
