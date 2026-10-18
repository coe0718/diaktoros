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
                     "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "rev", "id": 7}}], ""

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

    def test_issue_fix_hold_does_not_exclude_its_origin_pr(self):
        """#759: an issue waiting for PR #1 to merge must not keep #1 off the ready list."""
        from diaktoros import fix_hold
        self.add(1)
        self.st.fix_hold_set(12, 1)
        text = self.run_ready()
        self.assertIn("1. #1", text)
        self.assertNotIn("not ready", text)
        self.assertNotIn("fix waits", text)
        self.assertEqual(self.st.fix_holds(), {12: 1})      # the issue stays held
        released = []
        with mock.patch.object(config, "issue_fixes_enabled", return_value=True), \
             mock.patch.object(fix_hold, "pr_state", return_value="open") as pr_state, \
             mock.patch.object(fix_hold, "hold"), \
             mock.patch.object(fix_hold.issue_deps, "blocking", return_value=[]), \
             mock.patch.object(fix_hold, "_release",
                               side_effect=lambda *a, **k: released.append(a[2])):
            fix_hold.sweep(self.loop, self.st)
            self.assertEqual(released, [])                  # still open: not released
            pr_state.return_value = "merged"
            fix_hold.sweep(self.loop, self.st)
        self.assertEqual(released, [12])                    # released once #1 merged

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

    def retarget_ready(self, receipts, baseline=()):
        """Run ready on PR 3 held by a same-head retarget; ``receipts`` is the host receipt read."""
        self.add(3)
        # A hold's baseline is immutable, so every call starts from its own state directory.
        self.loop["state_dir"] = tempfile.mkdtemp(dir=self.tmp.name)
        self.st = state_mod.state_for(self.loop)
        self.st.transition_set(3, {"head": HEAD[3], "from_base": "b1", "at": 1.0,
                                   "old_review_ids": list(baseline)})
        with mock.patch.object(ready.transition, "read_receipts", return_value=receipts):
            return self.run_ready()

    def test_retarget_hold_with_receipted_approval_is_ready(self):
        """#791: the hold is owed a fresh review; a receipted approval at the head ends the veto."""
        text = self.retarget_ready({3: {"principal_id": 7, "verdict": "APPROVED"}})
        self.assertIn("1. #3", text)
        self.assertNotIn("retarget hold", text)

    def test_retarget_hold_without_receipted_approval_still_blocks(self):
        cases = (("no receipt", {}, ()),
                 ("receipt for another verdict",
                  {3: {"principal_id": 7, "verdict": "CHANGES_REQUESTED"}}, ()),
                 ("approval predates the retarget",
                  {3: {"principal_id": 7, "verdict": "APPROVED"}}, (3,)))
        for label, receipts, baseline in cases:
            with self.subTest(label):
                text = self.retarget_ready(receipts, baseline)
                self.assertNotIn("1. #3", text)
                self.assertIn("same-head base retarget hold", text)

    def test_retarget_hold_without_baseline_never_ready(self):
        self.add(3)
        self.st.transition_set(3, {"head": HEAD[3], "from_base": "b1", "at": 1.0,
                                   "old_review_ids": None})
        text = self.run_ready()
        self.assertNotIn("1. #3", text)
        self.assertIn("same-head base retarget hold", text)


if __name__ == "__main__":
    unittest.main()
