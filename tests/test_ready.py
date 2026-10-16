"""#479: `hermes dk ready` lists what is safe to merge, in order, and why the rest is not.
Driven through ``cmd_ready`` (the enforcing entry point) with GitHub reads stubbed."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)

import io
import pathlib
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import ci, cli, config, gh, ready, state as state_mod  # noqa: E402

HEAD = {n: f"{n:040x}" for n in range(1, 10)}


class Ready(unittest.TestCase):
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
        self.prs: dict = {}
        self.approved: dict = {}
        self.ci: dict = {}
        self.files: dict = {}
        self.mergeable: dict = {}

    def add(self, number, base="main", files=("a.py",), approved=True, ci_state=None,
            mstate="clean", draft=False):
        self.prs[number] = {"number": number, "title": f"pr {number}", "draft": draft,
                            "user": {"login": "fix"}, "base": {"ref": base},
                            "head": {"sha": HEAD[number], "ref": f"b{number}"}}
        self.approved[number] = approved
        self.ci[number] = ci_state or ci.CIState(passed=["tests"])
        self.files[number] = list(files)
        self.mergeable[number] = {"mergeable": mstate != "dirty", "mergeable_state": mstate,
                                  "number": number, "state": "open", "draft": draft,
                                  "head": {"sha": HEAD[number]}, "base": {"ref": base}}

    def run_ready(self):
        def reviews(_loop, number, login=None):
            if not self.approved[number]:
                return [], ""
            return [{"id": number, "state": "APPROVED", "commit_id": HEAD[number],
                     "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "rev"}}], ""

        def fetch(_loop, path, *a, **k):
            return self.mergeable[int(path.rsplit("/", 1)[1])], ""

        out = io.StringIO()
        with mock.patch.object(gh, "open_prs_read",
                               return_value=(list(self.prs.values()), "")), \
             mock.patch.object(gh, "reviews_read", side_effect=reviews), \
             mock.patch.object(gh, "fetch", side_effect=fetch), \
             mock.patch.object(gh, "pr_files_read", side_effect=lambda _l, n: (
                 [{"filename": f} for f in self.files[n]], "")), \
             mock.patch.object(ci, "read", side_effect=lambda _l, head: self.ci[
                 [n for n, h in HEAD.items() if h == head][0]]), \
             mock.patch.object(config, "load_id", return_value=self.loop), \
             mock.patch.object(state_mod, "state_for", return_value=self.st), \
             mock.patch.object(ready.gate_failures, "open_for", return_value=[]), \
             redirect_stdout(out):
            rc = cli.cmd_ready(types.SimpleNamespace(loop="w"))
        self.assertEqual(rc, 0)
        return out.getvalue()

    def test_clean_approved_pr_is_listed(self):
        self.add(1)
        text = self.run_ready()
        self.assertIn("1. #1", text)
        self.assertNotIn("not ready", text)

    def test_each_exclusion_is_shown_with_its_reason(self):
        self.add(1, approved=False)
        self.add(2, ci_state=ci.CIState(failed=["tests"]))
        self.add(3, mstate="dirty")
        self.add(4, draft=True)
        self.add(5)
        self.st.human_hold_set(5, HEAD[5], "touches human_paths")
        self.add(6)
        self.st.watch_save({"stale_approvals": {"6": {"head": HEAD[6], "why": "tests is red"}}})
        text = self.run_ready()
        self.assertNotIn("1. #", text)
        for needle in ("#1", "not approved at head", "#2", "failed:", "#3",
                       "conflicts with its base", "#4", "draft PR", "#5",
                       "waiting for a person", "#6", "is stale"):
            self.assertIn(needle, text)

    def test_stacked_base_comes_first_and_child_waits_for_it(self):
        self.add(2, base="b1", files=("c.py",))
        self.add(1, files=("a.py",))
        text = self.run_ready()
        self.assertLess(text.index("1. #1"), text.index("2. #2"))
        self.assertIn("#2 (after #1)", text)
        # parent not ready: the child is excluded with that reason
        self.approved[1] = False
        text = self.run_ready()
        self.assertNotIn("2. #2", text)
        self.assertIn("stacked on #1, which is not ready", text)

    def test_same_file_prs_are_one_after_another(self):
        self.add(3, files=("x.py", "y.py"))
        self.add(2, files=("x.py",))
        self.add(4, files=("z.py",))
        text = self.run_ready()
        self.assertIn("1. #2", text)
        self.assertIn("same files as #2 (x.py): merge after it", text)
        self.assertNotIn("same files as #3", text)

    def test_unreadable_ci_and_listing_are_not_guessed(self):
        self.add(1)
        self.ci[1] = None
        text = self.run_ready()
        self.assertIn("CI unreadable", text)
        out = io.StringIO()
        with mock.patch.object(gh, "open_prs_read", return_value=(None, "HTTP 500")), \
             mock.patch.object(config, "load_id", return_value=self.loop), \
             mock.patch.object(state_mod, "state_for", return_value=self.st), \
             redirect_stdout(out):
            self.assertEqual(cli.cmd_ready(types.SimpleNamespace(loop="w")), 0)
        self.assertIn("unknown: the open PR list could not be read (HTTP 500)", out.getvalue())

    def test_detail_that_changed_since_listing_is_not_ready(self):
        changes = {"number": 2, "state": "closed", "draft": True,
                   "head": {"sha": "f" * 40}, "base": {"ref": "release"}}
        for key, value in changes.items():
            with self.subTest(key):
                self.add(1)
                self.mergeable[1][key] = value
                text = self.run_ready()
                self.assertNotIn("1. #1", text)
                self.assertIn("changed since listing", text)


if __name__ == "__main__":
    unittest.main()
