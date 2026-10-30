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
        def fetch(loop, path, method="GET", body=None, login=None):
            data = api(loop, path, method, body, login)
            return data, ("" if data is not None else "unreadable")
        for target, name, value in ((gh, "api", api), (gh, "fetch", fetch),
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

    def _busy_close(self, tail):
        """Closed #5: an old close by a commit main contains on page 1, then filler, then tail."""
        self.issues[5] = {**self.issues[5], "state": "closed"}
        filler = [{"event": "labeled"}] * 100
        pages = {1: [{"event": "closed", "commit_id": MERGE}] + filler[1:], 2: filler, 3: filler}
        pages.update(tail)
        self.requested = []

        def api(loop, path, method="GET", body=None, login=None):
            if "/events" in path:
                page = int(path.split("page=")[-1]) if "&page=" in path else 1
                self.requested.append(page)
                return pages.get(page)
            if "/compare/" in path:
                return {"status": "ahead"}
            return self.issues[5]
        return api

    def test_a_close_beyond_the_page_bound_is_unknown_not_met(self):
        api = self._busy_close({4: [{"event": "reopened"},
                                    {"event": "closed", "commit_id": None}]})
        with mock.patch.object(gh, "fetch", lambda l, p, login=None: (api(l, p), "")), \
                mock.patch.object(gh, "api", api):
            done, why = issue_deps.met(self.loop, 5)
        self.assertIsNone(done)
        self.assertIn("could not be read in full", why)

    def test_an_unreadable_final_page_holds_the_dependent(self):
        api = self._busy_close({3: None})                 # the last page read fails
        self.depends(12, on=5)
        with mock.patch.object(gh, "fetch", lambda l, p, login=None: (api(l, p), "")), \
                mock.patch.object(gh, "api", api):
            blockers = issue_deps.blocking(self.loop, self.st, 12)
        self.assertEqual([b[0] for b in blockers], [5])

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

    def concurrent_second_caller(self, again):
        """Patch gh.api so a second process runs ``again()`` while the first one's comment POST is
        in flight (after its check, before its write): the window the issue describes."""
        inner, ran = gh.api, []

        def api(loop, path, method="GET", body=None, login=None):
            if method == "POST" and not ran:
                ran.append(1)
                again()
            return inner(loop, path, method, body, login)
        patcher = mock.patch.object(gh, "api", api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_processes_waiting_post_the_dependency_comment_once(self):
        blockers = [(5, "#5 is still open")]
        other = state_mod.state_for(self.loop)                   # the other process's view
        self.concurrent_second_caller(lambda: issue_deps.wait(self.loop, other, 12, blockers))
        issue_deps.wait(self.loop, self.st, 12, blockers)
        self.assertEqual(len([p for p, _b, _l in self.posted if p.endswith("/issues/12/comments")]), 1)
        self.assertTrue(self.st.dep_wait_said(12))

    def test_a_failed_dependency_comment_is_released_for_a_retry(self):
        blockers = [(5, "#5 is still open")]
        with mock.patch.object(gh, "api", lambda *a, **k: None):
            issue_deps.wait(self.loop, self.st, 12, blockers)
        self.assertFalse(self.st.dep_wait_said(12))
        issue_deps.wait(self.loop, self.st, 12, blockers)
        self.assertEqual(len(self.posted), 1)

    def test_two_processes_holding_post_the_pr_wait_comment_once(self):
        other = state_mod.state_for(self.loop)
        # The observer logs to stderr, which splits the `... ok` line `unittest -v` prints and
        # makes a reproduction proof read this test as missing; the notice is not under test.
        patcher = mock.patch.object(fix_hold, "_notice")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.concurrent_second_caller(lambda: fix_hold.hold(self.loop, other, 12, 7))
        fix_hold.hold(self.loop, self.st, 12, 7)
        self.assertEqual(len([p for p, _b, _l in self.posted if p.endswith("/issues/12/comments")]), 1)
        self.assertTrue(self.st.fix_hold_said(12))

    def claim_write_fails(self):
        """Only the one-time-notice claim's own write fails (``*-said.json``). Other state writes
        go through: the hold record is written strictly since #888/#889, so failing every write
        would test that write, not the claim (Tuck's ruling on #887)."""
        real = state_mod._atomic_write

        def write(path, data):
            if str(path).endswith("-said.json"):
                raise OSError("disk full")
            return real(path, data)
        return mock.patch.object(state_mod, "_atomic_write", side_effect=write)

    def test_a_dependency_comment_is_not_posted_when_the_claim_cannot_be_written(self):
        blockers = [(5, "#5 is still open")]
        with self.claim_write_fails():
            self.assertFalse(self.st.dep_wait_say(12))
            issue_deps.wait(self.loop, self.st, 12, blockers)
        self.assertEqual(self.posted, [])
        self.assertFalse(self.st.dep_wait_said(12))
        issue_deps.wait(self.loop, self.st, 12, blockers)       # disk back: the retry posts once
        self.assertEqual(len(self.posted), 1)

    def test_a_pr_wait_comment_is_not_posted_when_the_claim_cannot_be_written(self):
        patcher = mock.patch.object(fix_hold, "_notice")
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.claim_write_fails():
            self.assertFalse(self.st.fix_hold_say(12))
            fix_hold.hold(self.loop, self.st, 12, 7)
        self.assertEqual([p for p, _b, _l in self.posted if p.endswith("/issues/12/comments")], [])
        fix_hold.hold(self.loop, self.st, 12, 7)
        self.assertEqual(len([p for p, _b, _l in self.posted if p.endswith("/issues/12/comments")]), 1)

    def test_status_names_the_wait(self):
        self.depends(12, on=5)
        self.run_gate(self.labeled())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), \
                mock.patch.object(config, "readable_loops", lambda: ([self.loop], [])):
            cli.cmd_status(argparse.Namespace(loop="widgets", seat=None, json=False))
        self.assertIn("issue #12 fix waits for prerequisite #5", out.getvalue())

    # -- a dependency declared after the fix was queued (#775) --------------------------------

    def authorize(self):
        from diaktoros import broker
        inner = gh.api

        def api(loop, path, *a, **k):
            if path == "/user":                # each identity its own account (C2)
                who = k.get("login") or ""
                return {"login": who, "id": sum(map(ord, who))}
            return inner(loop, path, *a, **k)
        names = {"reviewer": "rev", "fixer": "fix"}
        with mock.patch.object(gh, "api", api), \
                mock.patch.object(config, "issue_fixes_enabled", lambda loop: True), \
                mock.patch.object(config, "seat_login", lambda loop, seat: names[seat]), \
                mock.patch.object(gh, "token_path",
                                  lambda loop, ident: Path(f"/tok/{ident}")), \
                mock.patch.object(gh, "token", lambda loop, ident: "t"):
            return broker.authorize_issue_fix(self.loop, repo=REPO, number=12)

    def claim_holds(self):
        from diaktoros import run_supervisor
        read = run_supervisor._ClaimRead()
        with mock.patch.object(config, "by_repo", lambda repo: self.loop):
            run_supervisor.Supervisor._claim_read_live(
                None, {"seat": "issue_fixer", "repo": REPO, "pr": 12}, read)
        return read.retry_read

    def test_the_broker_refuses_a_publish_while_a_later_dependency_is_unmet(self):
        from diaktoros import broker
        self.assertEqual(self.authorize(), "fix")                # nothing declared
        self.st.issue_dep_add(12, 5, {"by": "operator"})         # declared after enqueue
        with self.assertRaisesRegex(broker.BrokerDenied, "waiting for #5"):
            self.authorize()
        self.depends(12, waive=5)                                # waived: resumes
        self.assertEqual(self.authorize(), "fix")

    def test_a_queued_row_waits_at_claim_and_resumes_once_merged(self):
        self.assertFalse(self.claim_holds())
        self.st.issue_dep_add(12, 5, {"by": "operator"})
        self.assertTrue(self.claim_holds())
        self.close_a(MERGE)
        self.assertFalse(self.claim_holds())

    def test_the_launch_prompt_is_refused_during_preparation(self):
        from diaktoros import run_supervisor
        from diaktoros.run_supervisor import issue_inputs
        with mock.patch.object(issue_inputs, "issue_fix_issue",
                               lambda loop, n: self.issues[n]):
            self.st.issue_dep_add(12, 5, {"by": "operator"})
            with self.assertRaises(run_supervisor.RetryableError):
                issue_inputs.issue_fix_dependencies(self.loop, 12)

    def test_an_unreadable_dependency_holds_the_queued_row(self):
        self.st.issue_dep_add(12, 5, {"by": "operator"})
        self.issues.pop(5)                                       # GitHub cannot answer
        self.assertTrue(self.claim_holds())
        with mock.patch.object(issue_deps, "blocking", side_effect=OSError("ledger")):
            self.assertIn("unreadable", issue_deps.hold_reason(self.loop, 12))

    def test_depends_says_a_queued_turn_is_held_and_a_running_one_is_not_recalled(self):
        out = self.depends(12, on=5)[1]
        self.assertIn("already queued is held at launch and publish", out)
        self.assertIn("already running is not recalled", out)


if __name__ == "__main__":
    unittest.main()
