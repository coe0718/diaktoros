"""Review on #2578, F1: under a short watchdog budget a later page of the triage recovery
listing is read whenever it fits a window that leaves the rest of the run its share
(``gh_paging``), and a listing the budget cuts is read on from where it stopped next sweep, so an
eligible issue past page 1 is reached even when page 1 always takes most of the budget. A page
that runs past its window keeps the pages read; it never raises ``GateBudgetExceeded``.

Driven through ``watchdog.sweep_loop`` with the real ``gh.open_issues_read``: page 1 is 100 PRs
(never candidates) and the eligible issue is on page 2.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_triage_sweep as tts  # noqa: E402
from diaktoros import gh  # noqa: E402

PRS = [tts.issue(900 + n, pr=True) for n in range(gh.REVIEW_PAGE_SIZE)]


class ResumedSweep(tts.SweepBase):
    def run_sweep(self, left_after_page1: float, page2=None, budget: float = 20.0):
        """One sweep; page 1 leaves ``left_after_page1`` seconds. ``page2(path)`` answers page 2."""
        seen = []

        def fetch(loop, path, *args, **kw):
            if "/issues?" not in path:
                return [], None               # the sweep's other reads: nothing to say
            seen.append(path)
            if "&page=" not in path:
                scope["deadline"] = time.monotonic() + left_after_page1
                return PRS, None
            if path.endswith("&page=2"):
                return page2(path) if page2 else ([tts.issue(8)], None)
            return [], None

        with gh.gate_scope(time.monotonic() + budget, per_call=min(20.0, budget)) as scope, \
                mock.patch.object(gh, "fetch", side_effect=fetch):
            lines = self.sweep(None, read=gh.open_issues_read)
        return [line for line in lines if "⚠️" in line and "recovery sweep" in line], seen, scope

    def queued(self):
        return [e[1] for e in self.enqueued]

    def test_page_1_taking_over_half_still_reads_a_fast_page_2(self):
        # Vex's case: 20 s budget, 9 s left after page 1; the old half-budget reserve refused it.
        for _sweep in range(2):
            alerts, seen, _ = self.run_sweep(9.0)
            self.assertEqual((alerts, len(seen)), ([], 2))
        self.assertEqual(self.queued(), [8])

    def test_a_cut_listing_reads_on_next_sweep_then_starts_over(self):
        # Page 1 always leaves too little for another page: the next sweep starts at page 2.
        alerts, seen, _ = self.run_sweep(2.0)
        self.assertEqual((len(alerts), len(seen), self.queued()), (1, 1, []))
        self.assertIn("the next sweep reads on from there", alerts[0])
        alerts, seen, _ = self.run_sweep(2.0)
        self.assertEqual((alerts, self.queued()), ([], [8]))
        self.assertTrue(seen[0].endswith("&page=2"))
        alerts, seen, _ = self.run_sweep(15.0)         # whole: the next one starts from page 1
        self.assertNotIn("&page=", seen[0])

    def test_a_slow_page_2_keeps_page_1_and_the_rest_of_the_budget(self):
        # Vex's second probe: at 11 s left page 2 was given 11 s and its timeout raised out of
        # the sweep. Now its call is capped short of the run's share, and running out keeps page 1.
        from diaktoros import gh_paging    # not at import: the old head lacks it (fail-before)
        given, skew, real = [], [0.0], time.monotonic

        def slow(path):                                 # its call ran its whole timeout
            given.append(gh._budgeted("GET", path))
            skew[0] += given[0] + 0.1
            return None, "timed out"
        with mock.patch("time.monotonic", lambda: real() + skew[0]):
            alerts, seen, scope = self.run_sweep(11.0, page2=slow)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(alerts), 1, alerts)
        self.assertIn("open issue page 2 not read within the", alerts[0])
        self.assertLessEqual(given[0], 11.0 - gh_paging.MIN_PAGE_S)
        self.assertGreater(scope["deadline"] - time.monotonic() - skew[0], 0)   # the rest's time

    def test_a_resume_point_that_could_not_be_saved_is_never_promised(self):
        # Review on #2578, F3: the save was best effort (``kv_set``), yet the alert promised the
        # next sweep would read on. Now a failed save clears the old point and says page 1.
        from diaktoros import triage_sweep
        path, save = self.st.dir / triage_sweep.RESUME_FILE, self.st._save_strict

        def fail(where, data):
            if where == path:
                raise OSError("No space left on device")
            return save(where, data)
        with mock.patch.object(self.st, "_save_strict", side_effect=fail):
            alerts, _, _ = self.run_sweep(2.0)          # cut at page 2, its stop not saved
        self.assertEqual(len(alerts), 1, alerts)
        self.assertNotIn("reads on from there", alerts[0])
        self.assertIn("could not be saved, so the next sweep starts again from page 1", alerts[0])
        self.assertEqual(triage_sweep.resume_page(self.st), 1)      # and so it does
        save(path, {"next": 2})                          # an older stop neither saved nor cleared
        with mock.patch.object(self.st, "_save_strict", side_effect=fail), \
                mock.patch.object(Path, "unlink", side_effect=OSError("Read-only file system")):
            alerts, seen, _ = self.run_sweep(15.0)      # from page 2, whole: page 1 not saved
        self.assertTrue(seen[0].endswith("&page=2"))
        self.assertEqual(len(alerts), 1, alerts)
        self.assertIn("could not reset where its listing starts", alerts[0])


if __name__ == "__main__":
    unittest.main()
