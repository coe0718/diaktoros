"""Conflicts are found soon after the merge that caused them, not one or two sweeps later.

GitHub computes a PR's mergeability lazily: the first read after its base moves usually answers
"unknown", and the sweep took that as "no conflict" — so a conflict waited for the next sweep (15
minutes), often the one after. Now the sweep re-reads an unknown answer once, after one short
wait; and a merge into the base asks for a conflict-only scan a minute later (``conflict_scan``,
started by the reviewer gate), which acts exactly as the sweep's conflict step does.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import fcntl
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import conflict_scan, gate, gh, pr_hold  # noqa: E402
from diaktoros import watch_mode  # noqa: E402

TIP = "c" * 40
UNKNOWN = {**fg.LIVE, "mergeable": None, "mergeable_state": "unknown"}
DIRTY = {**fg.LIVE, "mergeable": False, "mergeable_state": "dirty"}
CLEAN = {**fg.LIVE, "mergeable": True, "mergeable_state": "clean"}


class Detection(fg.Base):
    def setUp(self):
        super().setUp()
        self.set_push(True)

    def patches(self, reads, hold=(None, ""), prs=None, armed=True):
        from scripts import watchdog
        enqueue = mock.patch.object(gate, "enqueue_isolated", return_value="queued")
        return watchdog, enqueue, [
            mock.patch.object(watch_mode, "TEST", armed is True),
            mock.patch.object(gate, "hooks_read", return_value=(armed, "")),
            mock.patch.object(gate, "hooks_armed", return_value=True),
            mock.patch.object(watchdog.route_intent, "heal", return_value=[]),
            mock.patch.object(gh, "open_prs", return_value=[fg.LIVE] if prs is None else prs),
            mock.patch.object(gh, "pr", side_effect=list(reads)),
            mock.patch.object(gh, "reviews", return_value=[fg.verdict()]),
            mock.patch.object(pr_hold, "read", return_value=hold),
            mock.patch.object(watchdog, "base_tip", return_value=TIP),
            mock.patch.object(watchdog, "retry_pending_breaches"),
            mock.patch.object(watchdog.routes, "fire"),
            mock.patch.object(watchdog.observer, "notify"),
            mock.patch.object(watchdog.observer, "retry", return_value=0),
            mock.patch.object(watchdog.observer, "flush")]

    def run_with(self, call, reads, **kw):
        watchdog, enqueue, patches = self.patches(reads, **kw)
        with enqueue as queued:
            for patch in patches:
                patch.start()
            try:
                call(watchdog)
            finally:
                for patch in reversed(patches):
                    patch.stop()
        return queued

    def sweep(self, reads):
        return self.run_with(lambda w: w.sweep_loop(self.loop, self.st), reads)

    def test_a_conflict_github_has_not_computed_yet_is_found_in_the_same_sweep(self):
        self.sweep([CLEAN] * 4)                     # the first sweep only snapshots heads
        queued = self.sweep([UNKNOWN, DIRTY] + [DIRTY] * 4)
        conflict = [c for c in queued.call_args_list if "conflict" in c.kwargs.get("turn_key", "")]
        self.assertEqual(len(conflict), 1, queued.call_args_list)
        self.assertEqual(conflict[0].kwargs["turn_key"].split(":")[-1], TIP)

    def test_the_scan_acts_on_a_conflict_as_the_sweep_does_and_leaves_a_held_pr(self):
        found = []
        queued = self.run_with(lambda w: found.append(w.scan_conflicts(self.loop, self.st)),
                               [UNKNOWN, DIRTY])
        self.assertEqual(found, [1])
        (call,) = queued.call_args_list
        self.assertEqual((call.args[1:3], call.kwargs["turn_key"].split(":")[-1]),
                         (("fixer", 7), TIP))
        queued = self.run_with(lambda w: w.scan_conflicts(self.loop, self.st), [DIRTY],
                               hold=({"by": "boss"}, ""))
        queued.assert_not_called()


class ScanFilters(Detection):
    """Review on #1001: the scan acts only on the PRs the sweep does, and stops when it can't
    tell (paused hooks, an unreadable listing). Each condition removed turns this red."""

    def scan(self, prs, reads, **kw):
        found = []
        queued = self.run_with(lambda w: found.append(w.scan_conflicts(self.loop, self.st)),
                               reads, prs=prs, **kw)
        return found[0], queued

    def test_drafts_strangers_and_other_bases_get_no_turn_and_a_loop_pr_still_does(self):
        others = {"draft": {**fg.LIVE, "number": 8, "draft": True},
                  "stranger": {**fg.LIVE, "number": 9, "user": {"login": "someone"}},
                  "other base": {**fg.LIVE, "number": 10,
                                 "base": {"ref": "release", "sha": "b" * 40}}}
        for name, pr in others.items():
            with self.subTest(name):
                found, queued = self.scan([pr], [{**DIRTY, "number": pr["number"]}] * 2)
                self.assertEqual(found, 0)
                queued.assert_not_called()
        found, queued = self.scan(list(others.values()) + [fg.LIVE], [DIRTY])
        self.assertEqual(found, 1)
        self.assertEqual([c.args[2] for c in queued.call_args_list], [7])

    def test_paused_hooks_or_an_unreadable_listing_scan_nothing(self):
        found, queued = self.scan([fg.LIVE], [], armed=False)
        self.assertEqual(found, 0)
        queued.assert_not_called()
        with mock.patch.object(gh, "open_prs", return_value=None):
            from scripts import watchdog
            with mock.patch.object(watch_mode, "TEST", True), \
                    mock.patch.object(gh, "pr", side_effect=AssertionError("no PR read")):
                self.assertEqual(watchdog.scan_conflicts(self.loop, self.st), 0)


class Plumbing(fg.Base):
    def setUp(self):
        super().setUp()
        self.set_push(True)

    def test_the_gate_requests_a_scan_and_starts_the_detached_scanner(self):
        with mock.patch.object(gate.subprocess, "Popen") as popen:
            gate.schedule_conflict_scan(self.loop, self.st)
        self.assertIsNotNone(conflict_scan.due(self.st))
        argv = popen.call_args.args[0]
        self.assertEqual(argv[-3:], ["--loop", self.loop["id"], "--conflicts"])
        self.assertTrue(argv[1].endswith("scripts/watchdog.py"))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])   # never the gate's child

    def test_a_failed_start_is_logged_never_raised(self):
        with mock.patch.object(gate.subprocess, "Popen", side_effect=OSError("no fork")), \
                mock.patch.object(gate, "log") as log:
            gate.schedule_conflict_scan(self.loop, self.st)
        self.assertIn("the sweep checks", log.call_args.args[0])

    def test_the_watchdog_conflicts_mode_runs_only_the_scan(self):
        import argparse
        from scripts import watchdog
        args = argparse.Namespace(loop=self.loop["id"], drain=False, seat="reviewer",
                                  conflicts=True)
        with mock.patch.object(conflict_scan, "run", return_value=1) as ran, \
                mock.patch.object(watchdog, "sweep_loop") as swept:
            watchdog.run(args, 600.0)
        ran.assert_called_once()
        swept.assert_not_called()
        # Review on #1001: inside the process's GitHub budget, with half left for a scan.
        self.assertEqual(ran.call_args.kwargs["lifetime"], 300.0)


class Debounce(unittest.TestCase):
    """``conflict_scan.run``: one scan per burst of merges, another for a merge during it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.st = types.SimpleNamespace(dir=Path(tmp.name))
        self.now = 1000.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        hooks, self.during_sleep = self.during_sleep, []
        for hook in hooks:
            hook()

    def run_scan(self, scan):
        return conflict_scan.run(self.st, scan, clock=self.clock, sleep=self.sleep)

    def test_a_burst_of_merges_is_one_scan_after_the_last(self):
        conflict_scan.request(self.st, now=self.now)
        later = []
        self.during_sleep = [lambda: later.append(conflict_scan.request(self.st, now=self.now))]
        scans = []
        self.assertEqual(self.run_scan(lambda: scans.append(self.now)), 1)
        self.assertEqual(len(scans), 1)
        self.assertGreaterEqual(scans[0], later[0])       # at the later merge's due time

    def test_a_merge_during_the_scan_gets_another_scan(self):
        self.during_sleep = []
        conflict_scan.request(self.st, now=self.now)
        scans = []

        def scan():
            scans.append(self.now)
            if len(scans) == 1:
                conflict_scan.request(self.st, now=self.now)
        self.assertEqual(self.run_scan(scan), 2)

    def test_no_request_or_another_scanner_runs_nothing(self):
        self.during_sleep = []
        self.assertEqual(self.run_scan(lambda: self.fail("no request")), 0)
        conflict_scan.request(self.st, now=self.now)
        fd = os.open(conflict_scan.path(self.st).with_suffix(".lock"), os.O_WRONLY | os.O_CREAT)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.assertEqual(self.run_scan(lambda: self.fail("lock is held")), 0)

    def test_a_scanner_stops_at_its_lifetime(self):
        self.during_sleep = []

        def keep_pushing():
            conflict_scan.request(self.st, now=self.now)
            self.during_sleep.append(keep_pushing)
        conflict_scan.request(self.st, now=self.now)
        self.during_sleep = [keep_pushing]
        self.assertEqual(conflict_scan.run(self.st, lambda: self.fail("never due"),
                                           clock=self.clock, sleep=self.sleep, lifetime=300.0), 0)
        self.assertGreaterEqual(self.now - 1000.0, 300.0)
        self.assertLess(self.now - 1000.0, 300.0 + 31)


if __name__ == "__main__":
    unittest.main()
