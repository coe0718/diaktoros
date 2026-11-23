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


class Base(tts.SweepBase):
    def run_sweep(self, left_after_page1: float, page2=None, budget: float = 20.0, page1=PRS,
                  places=None):
        """One sweep; page 1 (``page1``) leaves ``left_after_page1`` seconds. ``page2(path)``
        answers page 2, and ``places`` the places before it a resumed sweep reads again (F6):
        by default, the end of ``page1``."""
        seen = []

        def fetch(loop, path, *args, **kw):
            if "/issues?" not in path:
                return [], None               # the sweep's other reads: nothing to say
            seen.append(path)
            if "per_page=25&page=4" in path:      # places 76 to 100, before page 2 (F6)
                return (page1 if places is None else places)[-25:], None
            if "&page=" not in path:
                scope["deadline"] = time.monotonic() + left_after_page1
                return page1, None
            if path.endswith("&page=2"):
                return page2(path) if page2 else ([tts.issue(8)], None)
            return [], None

        with gh.gate_scope(time.monotonic() + budget, per_call=min(20.0, budget)) as scope, \
                mock.patch.object(gh, "fetch", side_effect=fetch):
            lines = self.sweep(None, read=gh.open_issues_read)
        return [line for line in lines if "⚠️" in line and "recovery sweep" in line], seen, scope

    def queued(self):
        return [e[1] for e in self.enqueued]


class ResumedSweep(Base):

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
        self.assertTrue(seen[1].endswith("&page=2"))          # after the places before it
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
        from diaktoros import triage_resume as resume, triage_sweep
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
        self.assertEqual(triage_sweep.resume_point(self.st), (1, {}))      # and so it does
        save(path, {"next": 2, **resume.boundary(PRS)})  # an older stop neither saved nor cleared
        with mock.patch.object(self.st, "_save_strict", side_effect=fail), \
                mock.patch.object(Path, "unlink", side_effect=OSError("Read-only file system")):
            alerts, seen, _ = self.run_sweep(15.0)      # from page 2, whole: page 1 not saved
        self.assertTrue(seen[1].endswith("&page=2"))
        self.assertEqual(len(alerts), 1, alerts)
        self.assertIn("could not reset where its listing starts", alerts[0])

    def test_a_cut_page_is_read_again_until_every_candidate_on_it_is_queued(self):
        # Review on #2578, F4: the resume point moved past a cut page before its candidates were
        # queued, so those past the sweep's bound (or a worker that was down) were skipped.
        from diaktoros import triage_sweep
        page1 = [tts.issue(n, age=7200.0 + n) for n in range(100, 115)] + PRS[:85]   # and PRs

        def down(*_args, **_kwargs):
            raise RuntimeError("worker down")
        with mock.patch.object(self, "fake_enqueue", side_effect=down):
            alerts, _, _ = self.run_sweep(2.0, page1=page1)     # cut after page 1, none queued
        self.assertIn("still wait to be queued", alerts[0])
        self.assertEqual((self.queued(), triage_sweep.resume_point(self.st)[0]), ([], 1))
        alerts, seen, _ = self.run_sweep(2.0, page1=page1)      # page 1 again: ten of fifteen
        self.assertNotIn("&page=", seen[0])
        self.assertEqual((len(self.queued()), triage_sweep.resume_point(self.st)[0]), (10, 1))
        alerts, seen, _ = self.run_sweep(2.0, page1=page1)      # page 1 again: the last five
        self.assertNotIn("&page=", seen[0])
        self.assertEqual(sorted(self.queued()), list(range(100, 115)))
        self.assertIn("reads on from there", alerts[0])
        alerts, seen, _ = self.run_sweep(2.0, page1=page1)      # only now on to page 2
        self.assertTrue(seen[1].endswith("&page=2"))
        self.assertEqual((alerts, self.queued()[-1]), ([], 8))

    def test_a_slow_resumed_page_is_bounded_and_the_next_sweep_starts_over(self):
        # Review on #2578, F5: the page a sweep resumed at was exempt from the window, so a slow
        # one spent the whole budget and raised; and the cursor stayed on it, never page 1 again.
        from diaktoros import triage_resume, triage_sweep
        self.st.kv_set_strict(self.st.dir / triage_sweep.RESUME_FILE,
                              {"next": 2, **triage_resume.boundary(PRS)})
        given, skew, real = [], [0.0], time.monotonic

        def slow(path):                                 # its call ran its whole timeout
            given.append(gh._budgeted("GET", path))
            skew[0] += given[0] + 0.1
            if gh.remaining() <= 0:
                raise gh.GateBudgetExceeded("open issue page 2 timed out")
            return None, "timed out"
        with mock.patch("time.monotonic", lambda: real() + skew[0]):
            alerts, seen, scope = self.run_sweep(20.0, page2=slow)
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[1].endswith("&page=2"))
        self.assertLessEqual(given[0], 15.0)            # 20 s less the 5 s held back
        self.assertGreater(scope["deadline"] - time.monotonic() - skew[0], 0)   # the rest's time
        self.assertEqual(len(alerts), 1, alerts)
        self.assertIn("open issue page 2 not read within the", alerts[0])
        self.assertIn("the next sweep starts again from page 1", alerts[0])
        self.assertEqual(triage_sweep.resume_point(self.st)[0], 1)
        alerts, seen, _ = self.run_sweep(15.0)          # and it does: page 1, then page 2
        self.assertNotIn("&page=", seen[0])
        self.assertEqual((alerts, self.queued()), ([], [8]))


class ShiftedListing(Base):
    """Review on #2578, F6: the saved page is an offset into a list that changes. A PR before the
    cut closes, the eligible issue moves from place 101 onto page 1, and a sweep that resumed at
    page 2 skipped it, said nothing and started the next listing over past it."""

    def cut_then_close(self, closed: int):
        """Page 1 is 100 PRs, the issue is place 101; the sweep is cut there, then ``closed``
        PRs before it close. Returns the listing as the next sweep finds it."""
        self.run_sweep(2.0)
        self.assertEqual(self.queued(), [])
        return PRS[closed:] + [tts.issue(8)]

    def test_an_issue_that_moved_onto_the_page_before_is_read_again(self):
        now = self.cut_then_close(1)
        alerts, seen, _ = self.run_sweep(2.0, page1=now[:100], page2=lambda _: (now[100:], None))
        self.assertEqual((alerts, self.queued()), ([], [8]))   # 101 is now 100: read, queued
        self.assertIn("per_page=25&page=4", seen[0])           # in the places before page 2

    def test_more_closed_than_it_reads_again_starts_the_next_sweep_over(self):
        now = self.cut_then_close(30)                          # 71 left: places 76-100 empty
        alerts, _, _ = self.run_sweep(2.0, page1=now, page2=lambda _: ([], None), places=[])
        self.assertEqual(self.queued(), [])
        self.assertEqual(len(alerts), 1, alerts)
        self.assertIn("resumed its listing at page 2; issues/PRs before where it resumed closed",
                      alerts[0])
        self.assertIn("the next sweep starts again from page 1", alerts[0])
        alerts, seen, _ = self.run_sweep(2.0, page1=now)       # and it does: the issue is read
        self.assertNotIn("&page=", seen[0])
        self.assertEqual((alerts, self.queued()), ([], [8]))

    def test_the_kept_item_proves_the_places_reach_back_or_nothing_does(self):
        from diaktoros import triage_resume as resume
        kept = resume.boundary(PRS)                            # PR 999, at the same second
        self.assertEqual(kept["last"], 999)
        self.assertTrue(resume.reaches_back(kept, PRS[75:]))   # it is among them
        self.assertFalse(resume.reaches_back(kept, PRS[75:99]))   # closed, same-second ties
        self.assertTrue(resume.reaches_back(kept, [tts.issue(5, age=60.0)]))     # a newer first
        self.assertFalse(resume.reaches_back(kept, [tts.issue(5, age=7200.0)]))  # an older one
        self.assertFalse(resume.reaches_back({"next": 2}, PRS[75:]))   # nothing kept to reach


if __name__ == "__main__":
    unittest.main()
