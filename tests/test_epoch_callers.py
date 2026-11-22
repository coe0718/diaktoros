"""#2304: every ``util.epoch`` caller treats an unreadable time as unknown, never as fresh.

``util.epoch`` returns 0.0 for a missing or unreadable stamp (``util.iso_epoch`` returns None).
#563 was such a stamp read as "just now", so a stall check saw nothing stale. Each caller guards
the 0.0 by hand; ``GUARDED`` registers each one (``path::function``) with how it handles it and
the behaviour test below that feeds it a missing or garbage stamp. The scan fails on a caller
not registered, and on a registered one that is gone. ``util.age_min`` is still used (by
``scripts/watchdog.py``'s stall checks), so it stays: ``tests/test_util_time.py`` covers the
helper, and ``tests/test_epoch_age_decisions.py`` registers each decision that consumes it and
drives each one with a missing and a garbage time.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import doctor, fix_reply, gh, pr_hold, stats_public, triage_sweep  # noqa: E402
from diaktoros.doctor import cron_checks  # noqa: E402
from diaktoros.run_supervisor import views  # noqa: E402

BAD = (None, "", "garbage", "2026-13-99T99:99:99Z")
T = 1_790_000_000.0
STAMP = "2026-09-21T14:13:20Z"            # T

# path::function -> (how it handles epoch's 0.0, the test here that proves it, or why none).
GUARDED = {
    "diaktoros/util.py::age_min": ("0.0 is None: unknown, past every grace (#563); each "
                                   "watchdog decision that consumes it is tested there",
                                   "tests/test_epoch_age_decisions.py"),
    "diaktoros/fix_reply.py::_wake": ("only 0 < updated < began skips the comments: an "
                                      "unreadable update time reads them", "test_wake"),
    "diaktoros/fix_reply.py::newest_reply": ("an unreadable comment time is no reply after "
                                             "`since`", "test_newest_reply"),
    "diaktoros/doctor/cron_checks.py::check_watchdog_last_run": (
        "an unreadable last_run is UNKNOWN, never a fresh run", "test_watchdog_last_run"),
    "diaktoros/gh.py::_older_than": ("an unreadable created_at is not older than the floor: "
                                     "paging goes on", "test_paging"),
    "diaktoros/gh.py::open_issues_read": ("an unreadable `since` is no floor: paging goes on",
                                          "test_paging"),
    "diaktoros/run_supervisor/views.py::disputed": (
        "an unreadable verdict time never hides a dispute at this head", "test_disputed"),
    "diaktoros/pr_hold.py::decide": ("an unreadable event time sorts before every readable one",
                                     "test_hold_order"),
    "diaktoros/pr_hold.py::line": ("an unreadable hold time says 'an unknown time'",
                                   "test_hold_line"),
    "diaktoros/triage_sweep.py::candidates": (
        "an issue with an unreadable creation time cannot be placed in the grace-to-horizon "
        "window and is skipped, as one past the horizon is", "test_late_triage"),
    "scripts/watchdog.py::_observe_heads": (
        "a first-seen PR with an unreadable created_at is not new (no observed_at): it is "
        "treated as preexisting, never as just opened", "an eligibility mark, not a stall"),
}


def callers():
    """``{path::function}`` of every call of ``epoch`` / ``util.epoch`` in the code."""
    found = set()
    for path in sorted([*(ROOT / "diaktoros").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]):
        rel = path.relative_to(ROOT).as_posix()

        def walk(node, where):
            for child in ast.iter_child_nodes(node):
                inner = (child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                         else where)
                if isinstance(child, ast.Call):
                    func = child.func
                    name = func.id if isinstance(func, ast.Name) else (
                        func.attr if isinstance(func, ast.Attribute) else "")
                    if name == "epoch":
                        found.add(f"{rel}::{where}")
                walk(child, inner)
        walk(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


class EveryEpochCaller(unittest.TestCase):
    def test_every_caller_is_registered(self):
        self.assertEqual(sorted(callers() - set(GUARDED)), [],
                         "a new util.epoch caller: register how it handles 0.0 (a missing or "
                         "unreadable stamp) in GUARDED, with a test, or use util.iso_epoch")
        self.assertEqual(sorted(set(GUARDED) - callers()), [], "registered callers now gone")

    def test_every_named_test_exists(self):
        for key, (_, proof) in GUARDED.items():
            if proof.startswith("test_"):
                self.assertTrue(hasattr(self, proof), f"{key}: no {proof}")
            elif proof.startswith("tests/"):
                self.assertTrue((ROOT / proof).exists(), key)

    def test_wake(self):
        loop = {"triage": {"fix_label": "fix"}}
        for bad in BAD:
            live = {"labels": [{"name": "fix"}], "updated_at": bad}
            with self.subTest(bad=bad), \
                    mock.patch("diaktoros.run_supervisor.issue_inputs.issue_fix_issue",
                               return_value=live), \
                    mock.patch.object(fix_reply, "newest_reply", return_value=None), \
                    mock.patch.object(gh, "issue_comments_read", return_value=([], "")) as read:
                fix_reply._wake(loop, None, {"number": 4, "began": T})
                read.assert_called_once()        # not "nothing written since": comments read
        with mock.patch("diaktoros.run_supervisor.issue_inputs.issue_fix_issue",
                        return_value={**live, "updated_at": "2020-01-01T00:00:00Z"}), \
                mock.patch.object(gh, "issue_comments_read") as read:
            self.assertEqual(fix_reply._wake(loop, None, {"number": 4, "began": T}), "waiting")
            read.assert_not_called()             # control: a readable older update skips them

    def test_newest_reply(self):
        def comment(at):
            return {"id": 1, "user": {"login": "boss"}, "created_at": at}
        with mock.patch.object(fix_reply, "repliers", return_value={"boss"}):
            for bad in BAD:
                self.assertIsNone(fix_reply.newest_reply({}, [comment(bad)], T - 60), bad)
            self.assertIsNotNone(fix_reply.newest_reply({}, [comment(STAMP)], T - 60))

    def test_watchdog_last_run(self):
        for bad in ("garbage", "2026-13-99T99:99:99Z"):
            st = SimpleNamespace(watch=lambda bad=bad: {"last_run": bad})
            with self.subTest(bad=bad), mock.patch("diaktoros.state.state_for", return_value=st):
                check = cron_checks.check_watchdog_last_run({"id": "l"})
                self.assertEqual(check.status, doctor.UNKNOWN, check.detail)

    def test_paging(self):
        for bad in BAD:
            self.assertFalse(gh._older_than({"created_at": bad}, T), bad)
        self.assertTrue(gh._older_than({"created_at": "2020-01-01T00:00:00Z"}, T))
        with mock.patch.object(gh, "_read_pages", return_value=([], "")) as pages:
            gh.open_issues_read({"repo": "o/r"}, "garbage")
        self.assertFalse(pages.call_args.kwargs["created_floor"])
        self.assertFalse(gh._older_than({"created_at": "2020-01-01T00:00:00Z"}, 0.0))

    def test_disputed(self):
        with mock.patch.object(views, "dispute_at", return_value=T):
            for bad in BAD:
                self.assertEqual(views.disputed("db", "o/r", 7, "h", {"submitted_at": bad}), T)
            self.assertIsNone(views.disputed("db", "o/r", 7, "h", {"submitted_at": STAMP}))

    def test_hold_order(self):
        def said(body, at):
            return {"body": body, "user": {"login": "boss"}, "created_at": at}
        with mock.patch("diaktoros.config.maintainers", return_value={"boss"}):
            for bad in BAD:
                released = [said(pr_hold.HOLD, bad), said(pr_hold.RELEASE, STAMP)]
                self.assertIsNone(pr_hold.decide({}, 7, [], released), bad)
                held = [said(pr_hold.HOLD, STAMP), said(pr_hold.RELEASE, bad)]
                self.assertIsNotNone(pr_hold.decide({}, 7, [], held), bad)

    def test_hold_line(self):
        for bad in BAD:
            self.assertIn("an unknown time", pr_hold.line({"at": bad, "by": "boss"}), bad)

    def test_late_triage(self):
        loop = {"triage": {"fix_label": "fix"}}
        now = time.time()
        old_enough = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(now - triage_sweep.GRACE_S - 60))
        with mock.patch.object(triage_sweep.issue_inputs, "triage_blocker", return_value=""):
            for bad in BAD:
                issue = {"number": 5, "created_at": bad, "labels": []}
                self.assertEqual(triage_sweep.candidates(loop, [issue], set(), now), [], bad)
            issue = {"number": 5, "created_at": old_enough, "labels": []}
            self.assertEqual(triage_sweep.candidates(loop, [issue], set(), now), [5])

    def test_closed_listing_reads_an_unreadable_time_as_unknown(self):
        # Fixed with this test: closed_listing read a closed PR's null merged_at as epoch 0.0,
        # not None, so ``cycle`` counted an unmerged PR as merged; and an unreadable created_at
        # as 0.0, so its open-to-merge time was ~56 years.
        items = [{"number": 7, "created_at": STAMP,
                  "pull_request": {"merged_at": "2026-09-21T15:13:20Z"}},
                 {"number": 8, "created_at": STAMP, "pull_request": {"merged_at": None}},
                 {"number": 9, "created_at": "garbage",
                  "pull_request": {"merged_at": "2026-09-21T15:13:20Z"}}]
        with mock.patch.object(gh, "_read_pages", return_value=(items, "")):
            closed, partial = stats_public.closed_listing({"repo": "o/r"}, 0)
        self.assertEqual((closed[8]["merged"], closed[9]["created"]), (None, None))
        rows = {"reviews": [{"pr": n, "created": T} for n in (7, 8, 9)], "proofs": []}
        got = stats_public.cycle(rows, closed, T - 10, T + 10)
        self.assertEqual(got["rounds"]["n"], 2)                 # 7 and 9 merged; 8 did not
        self.assertEqual(got["open_to_merge"], stats_public.spread([3600.0]))


if __name__ == "__main__":
    unittest.main()
