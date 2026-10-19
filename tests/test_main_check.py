"""A required check red on main after merges: one notice per head, naming the merges.

``main_check.sweep`` with GitHub mocked: red sends one notice (check, merged PRs, the
up-to-date-branch advice), green sends nothing, the same head never notifies twice.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import ci, gh, main_check, observer, state as state_mod  # noqa: E402

LOOP = {"repo": "acme/widgets", "base": "main", "read_token": "reader", "required_checks": []}
RED = ci.CIState(failed=["tests"], passed=["lint"], urls={"tests": "https://x/run/1"})
GREEN = ci.CIState(passed=["tests", "lint"])
COMPARE = {"commits": [{"commit": {"message": "Fix a (#11)\n\nbody"}},
                       {"commit": {"message": "Merge pull request #12 from a/b"}},
                       {"commit": {"message": "direct push"}}],
           "total_commits": 3}


class FakeState:
    def __init__(self, path):
        self.dir = Path(path)
        self._real = state_mod.LoopState.__new__(state_mod.LoopState)

    def _load(self, path, default):
        return state_mod.LoopState._load(self._real, path, default)

    def _save(self, path, data):
        state_mod.LoopState._save(self._real, path, data)

    def locked(self):
        import contextlib
        return contextlib.nullcontext()


class MainCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.st = FakeState(self.tmp.name)
        self.notices = []

    def run_sweep(self, head, state, compare=COMPARE, loop=None):
        def api(_loop, path, **_kw):
            return {"sha": head} if "/commits/" in path else compare
        with mock.patch.object(gh, "api", side_effect=api), \
             mock.patch.object(ci, "read", return_value=state), \
             mock.patch.object(observer, "notify",
                               side_effect=lambda *a, **kw: self.notices.append((a, kw))):
            return main_check.sweep(loop or LOOP, self.st)

    def test_red_main_sends_one_notice_naming_check_and_merges(self):
        self.assertFalse(self.run_sweep("a" * 40, GREEN))
        self.assertTrue(self.run_sweep("b" * 40, RED))
        (args, kw), = self.notices
        self.assertEqual(args[2], "main_red")
        self.assertEqual(args[4], "b" * 40)
        for text in ("tests", "#11", "#12", "up to date"):
            self.assertIn(text, kw["outcome"])

    def test_save_keeps_the_key_another_writer_recorded_meanwhile(self):
        path = self.st.dir / main_check.FILE

        def other_writer(*_a, **_k):                  # lands between the sweep's load and save
            self.st._save(path, {"green": "g" * 40})
            return RED

        def api(_loop, p, **_kw):
            return {"sha": "b" * 40} if "/commits/" in p else COMPARE
        with mock.patch.object(gh, "api", side_effect=api), \
             mock.patch.object(ci, "read", side_effect=other_writer), \
             mock.patch.object(observer, "notify"):
            self.assertTrue(main_check.sweep(LOOP, self.st))
        self.assertEqual(self.st._load(path, {}), {"green": "g" * 40, "notified": "b" * 40})

    def test_green_main_sends_nothing(self):
        self.assertFalse(self.run_sweep("a" * 40, GREEN))
        self.assertEqual(self.notices, [])

    def test_the_same_head_does_not_notify_twice(self):
        self.assertTrue(self.run_sweep("b" * 40, RED))
        self.assertFalse(self.run_sweep("b" * 40, RED))
        self.assertEqual(len(self.notices), 1)
        self.assertTrue(self.run_sweep("c" * 40, RED))     # a new red head is a new notice
        self.assertEqual(len(self.notices), 2)

    def test_only_required_checks_count(self):
        loop = {**LOOP, "required_checks": ["lint"]}
        self.assertFalse(self.run_sweep("b" * 40, RED, loop=loop))
        self.assertEqual(self.notices, [])

    def test_unreadable_ci_is_no_notice(self):
        self.assertFalse(self.run_sweep("b" * 40, None))
        self.assertEqual(self.notices, [])

    def test_truncated_compare_is_reported_as_partial(self):
        truncated = {**COMPARE, "total_commits": 400}
        self.assertFalse(self.run_sweep("a" * 40, GREEN))
        self.assertTrue(self.run_sweep("b" * 40, RED, compare=truncated))
        (_args, kw), = self.notices
        for text in ("#11", "#12", "partial", "truncated", "3 of 400", "not the full list"):
            self.assertIn(text, kw["outcome"])

    def test_complete_compare_is_not_called_partial(self):
        self.assertFalse(self.run_sweep("a" * 40, GREEN))
        self.assertTrue(self.run_sweep("b" * 40, RED))
        (_args, kw), = self.notices
        self.assertNotIn("partial", kw["outcome"])
        self.assertIn("merged since main's last green head: #11, #12", kw["outcome"])

    def test_missing_total_commits_is_partial(self):
        no_total = {"commits": COMPARE["commits"]}
        self.assertFalse(self.run_sweep("a" * 40, GREEN))
        self.assertTrue(self.run_sweep("b" * 40, RED, compare=no_total))
        (_args, kw), = self.notices
        self.assertIn("partial", kw["outcome"])

    def test_truncated_compare_without_pr_merges_is_not_called_complete(self):
        """Truncated slice, no commit names a PR: must not claim "no PR merges since ..."."""
        direct = {"commits": [{"commit": {"message": "direct push"}}], "total_commits": 400}
        self.assertFalse(self.run_sweep("a" * 40, GREEN))
        self.assertTrue(self.run_sweep("b" * 40, RED, compare=direct))
        (_args, kw), = self.notices
        outcome = kw["outcome"]
        self.assertIn("no PR merges found in the part of the compare GitHub returned", outcome)
        self.assertIn("1 of 400 commits read", outcome)
        self.assertIn("not the full list", outcome)
        self.assertNotIn("no PR merges since main's last green head", outcome)

    def test_complete_compare_without_pr_merges_says_none_since_green(self):
        direct = {"commits": [{"commit": {"message": "direct push"}}], "total_commits": 1}
        self.assertFalse(self.run_sweep("a" * 40, GREEN))
        self.assertTrue(self.run_sweep("b" * 40, RED, compare=direct))
        (_args, kw), = self.notices
        self.assertIn("no PR merges since main's last green head", kw["outcome"])
        self.assertNotIn("truncated", kw["outcome"])

    def test_unnamed_merges_notice_names_no_pr_zero(self):
        """Through the real sweep and notify: no merges to name means no #0 and no /pull/0."""
        sent = []
        loop = {**LOOP, "id": "w", "observer": {"url": "https://hook.example/x"}}
        self.st.observations = self.st.dir / "observations.json"

        def api(_loop, path, **_kw):
            return {"sha": "b" * 40} if "/commits/" in path else {"commits": []}
        with mock.patch.object(gh, "api", side_effect=api), \
             mock.patch.object(ci, "read", return_value=RED), \
             mock.patch.object(observer, "configured", return_value=""), \
             mock.patch.object(observer, "subscribes", return_value=True), \
             mock.patch.object(observer, "_deliver",
                               side_effect=lambda *a, **kw: sent.append(a) or True):
            self.assertTrue(main_check.sweep(loop, self.st))
        (args,), = [(a,) for a in sent]
        text, url = args[3], args[9]
        self.assertNotIn("#0", text)
        self.assertNotIn("/pull/0", text + url)
        self.assertEqual(url, "https://github.com/acme/widgets")
        self.assertEqual(observer.block_for(loop, "main_red", 0, "h", text)["pr"], "")

    def test_a_real_pr_notice_still_links_the_pull_request(self):
        self.assertEqual(observer.link({"repo": "a/b"}, 7), "https://github.com/a/b/pull/7")
        self.assertEqual(observer.link({"repo": "a/b"}, 7, True), "https://github.com/a/b/issues/7")
        self.assertIn("#7", observer.summarize({"id": "w"}, "main_red", 7, "h" * 7))

    def test_event_is_registered(self):
        for table in (observer.EMOJI, observer.LABEL):
            self.assertIn("main_red", table)
        self.assertIn("main_red", observer.EVENTS)


if __name__ == "__main__":
    unittest.main()
