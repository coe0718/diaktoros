"""#473: an approval goes stale when a required check at the approved head is red, cancelled or
missing past its wait. Driven through the watchdog sweep (the enforcing entry point) and
``explain``; one notice per PR and head; a new head clears it; an unapproved PR is never flagged."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)

import pathlib
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import ci, gate, gh, stale_approval, state as state_mod  # noqa: E402
from scripts import watchdog  # noqa: E402

H1, H2 = "a" * 40, "b" * 40
NOW = time.time()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def review(state: str, head: str, at: float) -> dict:
    return {"id": 1, "state": state, "commit_id": head, "submitted_at": iso(at),
            "user": {"login": "rev"}}


class Stale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = {"id": "w", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "read_token": "rev", "reviewers": ["rev"], "reviewer_seat": "rev",
                     "fixers": ["fix"], "review_only": [], "required_checks": ["tests"],
                     "state_dir": str(pathlib.Path(self.tmp.name) / "state"), "cooldown_h": 6,
                     "ttl_min": 45, "inflight_ttl_min": 10,
                     "seats": {"reviewer": {"route": "w-review"}, "fixer": {"route": "w-fix"}}}
        self.st = state_mod.state_for(self.loop)
        self.notices: list = []
        watch = self.st.watch()               # an already-armed loop: a first sweep only snapshots
        watch["armed_since"] = NOW - 86400
        self.st.watch_save(watch)

    def pr(self, head=H1):
        return {"number": 7, "draft": False, "user": {"login": "fix"}, "title": "t",
                "base": {"ref": "main", "sha": "c" * 40}, "head": {"sha": head},
                "created_at": iso(NOW - 9999)}

    def sweep(self, ci_state, reviews, head=H1):
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(watchdog.gate, "hooks_read", return_value=(True, "")), \
             mock.patch.object(watchdog.gh, "open_prs", return_value=[self.pr(head)]), \
             mock.patch.object(watchdog.gh, "pr", return_value=self.pr(head)), \
             mock.patch.object(watchdog.gh, "reviews", return_value=reviews), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(watchdog.gate_shims, "heal", return_value=[]), \
             mock.patch.object(watchdog.gate_shims, "heal_watchdog_shim", return_value=[]), \
             mock.patch.object(watchdog.gate, "resume_isolated"), \
             mock.patch.object(watchdog.ci_fix, "sweep"), \
             mock.patch.object(watchdog.main_check, "sweep"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"), \
             mock.patch.object(watchdog.observer, "notify",
                               side_effect=lambda *a, **kw: self.notices.append((a, kw))), \
             mock.patch.object(ci, "read", return_value=ci_state):
            watchdog.sweep_loop(self.loop, self.st)
        return [n for n in self.notices if n[0][2] == "stale_approval"]

    def approved(self, head=H1, age=3600):
        return [review("APPROVED", head, NOW - age)]

    def test_red_flags_once_per_head_and_explain_shows_it(self):
        red = ci.CIState(failed=["tests"])
        got = self.sweep(red, self.approved())
        self.assertEqual(len(got), 1)
        self.assertIn("is red", got[0][1]["outcome"])
        self.assertTrue(stale_approval.is_stale(self.st.watch(), 7, H1))
        facts = {"pr": self.pr(), "reviews": self.approved(), "armed": True, "read_at": NOW}
        with mock.patch.object(gate.gate_failures, "open_for", return_value=[]):
            report = gate.explain(self.loop, self.st, 7, facts)
        self.assertIn("approval at aaaaaaa is stale", report["stale_approval"])
        self.assertIn(report["stale_approval"], report["blockers"])

    def test_the_notice_is_not_repeated_on_the_next_sweep(self):
        red = ci.CIState(failed=["tests"])
        self.sweep(red, self.approved())
        self.assertTrue(stale_approval.is_stale(self.st.watch(), 7, H1))
        # observer.notify dedups by (pr, head, event, identity): same identity both sweeps
        got = self.sweep(red, self.approved())
        self.assertEqual({n[1]["identity"] for n in got}, {"stale_approval"})
        self.assertEqual({n[0][4] for n in got}, {H1})

    def test_cancelled_flags(self):
        got = self.sweep(ci.CIState(cancelled=["tests"]), self.approved())
        self.assertEqual(len(got), 1)
        self.assertIn("cancelled", got[0][1]["outcome"])

    def test_missing_flags_only_past_its_wait(self):
        empty = ci.CIState(passed=["lint"])
        self.assertEqual(self.sweep(empty, self.approved(age=60)), [])
        self.assertFalse(stale_approval.is_stale(self.st.watch(), 7, H1))
        got = self.sweep(empty, self.approved(age=3600))
        self.assertEqual(len(got), 1)
        self.assertIn("never reported", got[0][1]["outcome"])

    def test_green_or_running_or_unreadable_is_not_flagged(self):
        for state in (ci.CIState(passed=["tests"]), ci.CIState(pending=["tests"]), None):
            self.assertEqual(self.sweep(state, self.approved()), [])

    def test_green_clears_a_recorded_finding(self):
        self.sweep(ci.CIState(failed=["tests"]), self.approved())
        self.sweep(ci.CIState(passed=["tests"]), self.approved())
        self.assertFalse(stale_approval.is_stale(self.st.watch(), 7, H1))

    def test_a_new_head_clears_it(self):
        self.sweep(ci.CIState(failed=["tests"]), self.approved())
        self.sweep(ci.CIState(passed=["tests"]), [], head=H2)
        self.assertFalse(stale_approval.is_stale(self.st.watch(), 7, H1))
        self.assertFalse(stale_approval.is_stale(self.st.watch(), 7, H2))

    def test_an_unapproved_pr_is_never_flagged(self):
        red = ci.CIState(failed=["tests"])
        for reviews in ([], [review("CHANGES_REQUESTED", H1, NOW - 60)],
                        [review("COMMENTED", H1, NOW - 60)],
                        [review("APPROVED", H2, NOW - 60)]):   # approval of another head
            self.assertEqual(self.sweep(red, reviews), [])
            self.assertFalse(stale_approval.is_stale(self.st.watch(), 7, H1))

    def test_assess_unit(self):
        self.assertEqual(stale_approval.assess(self.loop, ci.CIState(passed=["tests"]), 0, NOW), "")
        self.assertEqual(stale_approval.assess(self.loop, None, 0, NOW), "")


if __name__ == "__main__":
    unittest.main()
