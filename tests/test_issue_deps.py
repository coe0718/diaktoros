"""Issue dependencies a maintainer declares (#671): B's fix waits for A's implementation to be
merged into the base, through the same hand-off and sweep as every held issue fix.

GitHub is faked per path (issues, events, compare, pulls); the gate script, the loop state and
the sweep are real. ``issue_deps._fix_prs`` (the ledger's issue-fix PRs) is faked per test.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_issue_fix as tif  # noqa: E402
from diaktoros import cli, config, fix_hold, gh, issue_deps, state as state_mod  # noqa: E402

REPO, BASE = tif.REPO, tif.BASE
MERGE = "d" * 40
# The issue-fix gate fixture without its own tests, which assume its simpler GitHub fake.
GateFixture = type("GateFixture", (tif.Gate,),
                   {name: None for name in dir(tif.Gate) if name.startswith("test_")})


class Deps(GateFixture):
    def setUp(self):
        super().setUp()
        self.issues = {12: tif.issue(), 5: tif.issue(number=5, body="the prerequisite"),
                       6: tif.issue(number=6)}
        self.events, self.compare, self.pulls, self.fix_prs = {}, {}, {}, {}

        def api(loop, path, method="GET", body=None, login=None):
            if method == "POST":
                self.posted.append((path, body, login))
                return {"id": 1}
            if path.endswith(f"/git/ref/heads/{self.loop['base']}"):
                return {"ref": "refs/heads/main", "object": {"sha": BASE}}
            if "/compare/" in path:
                sha = path.split("/compare/")[1].split("...")[0]
                return {"status": self.compare[sha]} if sha in self.compare else None
            if "/events" in path:
                number = int(path.split("/issues/")[1].split("/")[0])
                return self.events.get(number, [])
            if "/pulls/" in path:
                return self.pulls.get(int(path.rsplit("/", 1)[1]))
            number = int(path.rsplit("/", 1)[1])
            return self.issues.get(number)
        for target, name, value in ((gh, "api", api),
                                    (issue_deps, "_fix_prs",
                                     lambda loop, n: self.fix_prs.get(n, [])),
                                    (config, "load_id", lambda loop_id: self.loop)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.st = state_mod.state_for(self.loop)

    def depends(self, issue=None, on=None, waive=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.cmd_depends(argparse.Namespace(loop="widgets", issue=issue, on=on,
                                                    waive=waive))
        return rc, out.getvalue()

    def close_a(self, commit_id: str | None, in_base: str = "ahead"):
        self.issues[5] = {**self.issues[5], "state": "closed"}
        self.events[5] = [{"event": "labeled"}, {"event": "closed", "commit_id": commit_id}]
        if commit_id:
            self.compare[commit_id] = in_base

    # -- declaring ----------------------------------------------------------------------------

    def test_declare_list_and_waive(self):
        self.assertEqual(self.depends(12, on=5)[0], 0)
        rc, out = self.depends(12, on=5)
        self.assertIn("already depends on #5", out)
        self.assertEqual(self.st.issue_deps(), {12: {5: self.st.issue_deps()[12][5]}})
        self.assertEqual(self.st.issue_deps()[12][5]["by"], "operator")
        rc, out = self.depends()
        self.assertIn("#12 depends on #5: waiting — #5 is still open", out)
        rc, out = self.depends(12, waive=5)
        self.assertEqual((rc, self.st.issue_deps()), (0, {}))
        self.assertEqual(self.depends(12, waive=5)[0], 1)       # nothing to waive

    def test_what_cannot_be_declared(self):
        self.assertIn("cannot depend on itself", self.depends(12, on=12)[1])
        self.depends(5, on=6)
        self.depends(6, on=12)
        rc, out = self.depends(12, on=5)                         # 12 → 5 → 6 → 12
        self.assertEqual(rc, 1)
        self.assertIn("refused: a cycle: #12 → #5 → #6 → #12", out)
        self.issues[7] = {**tif.issue(number=7), "pull_request": {}}
        rc, out = self.depends(12, on=7)
        self.assertEqual(rc, 1)
        self.assertIn("is a pull request", out)
        rc, out = self.depends(12, on=99)                        # unreadable: nothing declared
        self.assertEqual(rc, 2)
        self.assertNotIn(99, self.st.issue_deps().get(12, {}))

    # -- holding and releasing ----------------------------------------------------------------

    def test_labelled_while_the_prerequisite_is_open_spends_no_turn(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        self.assertEqual(self.enqueued, [])
        self.assertEqual(self.st.dep_waits(), [12])
        bodies = [b["body"] for _p, b, _l in self.posted]
        self.assertEqual(len(bodies), 1)
        self.assertIn("waiting for #5 — a maintainer made this issue depend on it", bodies[0])
        fix_hold.sweep(self.loop, self.st)                       # still open: no turn, no repeat
        self.assertEqual((self.enqueued, len(self.posted)), ([], 1))

    def test_a_merged_prerequisite_releases_it_once_against_the_base(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        self.close_a(MERGE)                                      # closed by a commit in main
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix")])
        self.assertEqual(self.st.dep_waits(), [])
        fix_hold.sweep(self.loop, self.st)                       # no second attempt
        self.assertEqual(len(self.enqueued), 1)

    def test_the_prerequisite_s_own_fix_pr_merged_into_the_base_counts(self):
        self.depends(12, on=5)
        self.fix_prs[5] = [40]
        self.pulls[40] = {"number": 40, "merged_at": "2026-10-14T00:00:00Z",
                          "base": {"ref": "main"}}
        self.run_gate(self.labeled())
        self.assertEqual(len(self.enqueued), 1)
        self.pulls[40] = {**self.pulls[40], "base": {"ref": "release"}}   # merged elsewhere
        self.assertIs(issue_deps.met(self.loop, 5)[0], False)

    def test_closed_without_a_merged_implementation_keeps_waiting_until_waived(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        for commit, where in ((None, ""), (MERGE, "diverged"), (MERGE, "behind")):
            with self.subTest(commit=commit, where=where):
                self.close_a(commit, where)
                fix_hold.sweep(self.loop, self.st)
                self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))
                done, why = issue_deps.met(self.loop, 5)
                self.assertIs(done, False)                      # known, not unreadable
                self.assertIn("closed without an implementation merged into main", why)
        self.depends(12, waive=5)                                # a person decides
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual(len(self.enqueued), 1)

    def test_unreadable_facts_never_start_blocked_work(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        self.issues.pop(5)                                       # the prerequisite unreadable
        fix_hold.sweep(self.loop, self.st)
        self.issues[5] = {**tif.issue(number=5), "state": "closed"}
        self.events[5] = [{"event": "closed", "commit_id": MERGE}]   # compare unreadable
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual((self.enqueued, self.st.dep_waits()), ([], [12]))

    def test_a_pr_wait_that_ends_hands_over_to_the_dependency_wait(self):
        self.depends(12, on=5)
        self.st.fix_hold_set(12, 7)                              # also held on a PR (#686)
        self.pulls[7] = {"number": 7, "state": "closed", "merged": True}
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual((self.enqueued, self.st.fix_holds(), self.st.dep_waits()),
                         ([], {}, [12]))

    def test_release_skips_an_issue_a_differently_named_pr_now_fixes(self):
        # prerequisite wait: a person's PR (any branch) opens while it waits
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        self.open_prs = [{"number": 30, "body": "Fixes #12", "head": {"ref": "human/my-branch"}}]
        self.close_a(MERGE)
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual((self.enqueued, self.st.dep_waits()), ([], []))
        self.assertTrue(any("PR #30 already fixes this" in b["body"] for _p, b, _l in self.posted))

    def test_pr_wait_release_skips_an_issue_an_open_pr_now_fixes(self):
        self.st.fix_hold_set(12, 7)
        self.pulls[7] = {"number": 7, "state": "closed", "merged": True}
        self.open_prs = [{"number": 30, "body": "Fixes #12", "head": {"ref": "human/x"}}]
        fix_hold.sweep(self.loop, self.st)
        self.assertEqual((self.enqueued, self.st.fix_holds()), ([], {}))

    def test_release_waits_when_the_open_pr_listing_is_unreadable(self):
        self.st.fix_hold_set(12, 7)
        self.pulls[7] = {"number": 7, "state": "closed", "merged": True}
        with mock.patch.object(gh, "open_prs_read", lambda loop: (None, "boom")):
            fix_hold.sweep(self.loop, self.st)
        self.assertEqual((self.enqueued, self.st.fix_holds()), ([], {12: 7}))
        fix_hold.sweep(self.loop, self.st)                       # readable again, none covers it
        self.assertEqual(len(self.enqueued), 1)

    def test_a_restart_keeps_the_dependency_and_the_wait(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        again = state_mod.state_for(config.normalize(self.raw))  # a new process
        self.assertEqual((list(again.issue_deps()), again.dep_waits()), ([12], [12]))
        self.assertTrue(again.dep_wait_said(12))

    def test_status_names_the_wait(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(config, "readable_loops", lambda: ([self.loop], [])):
            cli.cmd_status(argparse.Namespace(loop="widgets", seat=None, json=False))
        self.assertIn("issue #12 fix waits for prerequisite #5", out.getvalue())


if __name__ == "__main__":
    unittest.main()
