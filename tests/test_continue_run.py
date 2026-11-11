"""`continue` (#485): finish a half-written issue fix exactly once, never push, keep conflicting
facts quarantined, ignore a chat `continue` from anyone who is not a maintainer.

GitHub is faked at ``gh.fetch`` / ``gh.reviews_read`` and the two broker writes; the ledger is a
real SQLite file in a private temp dir.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diaktoros import broker, continue_run, gh, ledger, safe_push  # noqa: E402
from diaktoros.cli import operations  # noqa: E402
from diaktoros import config  # noqa: E402
from test_issue_fix import Base, BASE, REPO, issue  # noqa: E402

NEW = "d" * 40
BRANCH = "diaktoros/issue-12"


class Continue(Base):
    def setUp(self):
        super().setUp()
        self.sup, self.run_id = self.fix_row(state="uncertain")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET launch_intent=1,pid=NULL,error='post-write push "
                        "quarantine: pr_create_unknown' WHERE id=?", (self.run_id,))
        with ledger.connect(self.db) as con:
            con.execute("INSERT INTO issue_fixes(run_id,repo,number,base,kind,branch,state,"
                        "new_head,created,updated) VALUES(?,?,?,?,?,?,?,?,1,1)",
                        (self.run_id, REPO, 12, BASE, "pr", BRANCH, "pushed", NEW))
        self.branch_sha = NEW
        self.prs = []
        self.reviews = []
        self.writes = []
        self.ref_error = ""
        for target, fake in (
                (gh, mock.patch.object(gh, "fetch", self.fetch)),
                (gh, mock.patch.object(gh, "reviews_read", lambda loop, n, login=None:
                                       (self.reviews, ""))),
                (broker, mock.patch.object(broker, "open_issue_pr", self.open_pr)),
                (broker, mock.patch.object(broker, "request_issue_pr_review", self.request)),
                (broker, mock.patch.object(broker, "authorize_issue_fix", self.authorize)),
                (safe_push, mock.patch.object(safe_push, "open_branch", self.no_push))):
            fake.start()
            self.addCleanup(fake.stop)

    def authorize(self, loop, **kw):
        self.authorized = getattr(self, "authorized", 0) + 1
        return "fix"

    def no_push(self, *a, **k):
        self.writes.append("PUSH")
        raise AssertionError("continue must never push")

    def fetch(self, loop, path, method="GET", body=None, login=None):
        if "/git/ref/heads/" in path:
            if self.branch_sha is None:
                return None, "HTTP 404 Not Found"
            return {"object": {"sha": self.branch_sha}}, ""
        if "/pulls?" in path:
            return self.prs, ""
        return issue(), ""

    def open_pr(self, loop, **kw):
        self.writes.append("open_pr")
        self.prs.append(self.pr(77))
        return 77

    def request(self, loop, **kw):
        self.writes.append("request_review")
        self.prs[0]["requested_reviewers"] = [{"login": "review"}]

    def pr(self, number, **over):
        pr = {"number": number, "state": "open", "head": {"sha": self.branch_sha},
              "base": {"ref": self.loop["base"]}, "user": {"login": "fix"},
              "requested_reviewers": []}
        pr.update(over)
        return pr

    def run_state(self):
        with ledger.connect(self.db) as con:
            return con.execute("SELECT state FROM runs WHERE id=?", (self.run_id,)).fetchone()[0]

    def fix(self):
        return self.sup.issue_fix_result(self.run_id)

    def test_missing_pr_and_review_are_completed_once(self):
        ok, _ = continue_run.run(self.loop, self.sup, self.run_id)
        self.assertTrue(ok)
        self.assertEqual(self.writes, ["open_pr", "request_review"])
        self.assertEqual((self.fix()["state"], self.fix()["pr_number"]), ("requested", 77))
        self.assertEqual(self.run_state(), "failed")      # released
        again, lines = continue_run.run(self.loop, self.sup, self.run_id)   # exactly once
        self.assertEqual(self.writes, ["open_pr", "request_review"])
        self.assertTrue(again)

    def test_only_the_review_request_is_missing(self):
        self.prs = [self.pr(5)]
        with ledger.connect(self.db) as con:
            con.execute("UPDATE issue_fixes SET state='opened',pr_number=5")
        ok, _ = continue_run.run(self.loop, self.sup, self.run_id)
        self.assertTrue(ok)
        self.assertEqual(self.writes, ["request_review"])

    def as_app_reviewer(self):
        # The reviewer seat is a GitHub App identity (#1163): no PAT, and GitHub cannot request it.
        seat = self.loop["reviewer_seat"]
        self.loop = {**self.loop, "apps": {seat: {"app_id": 1, "key_file": "/k"}},
                     "tokens": {k: v for k, v in (self.loop.get("tokens") or {}).items()
                                if k.lower() != seat.lower()}}

    def open_pr_row(self, **over):
        self.prs = [self.pr(5, **over)]
        with ledger.connect(self.db) as con:
            con.execute("UPDATE issue_fixes SET state='opened',pr_number=5")

    def verdict(self, head, state="APPROVED"):
        return {"id": 1, "user": {"login": "review"}, "state": state, "commit_id": head}

    def test_an_app_reviewers_open_pr_gets_its_review_delivered_to_the_gate(self):
        # #1211: the PR is already open, so its `opened` will not come again, and GitHub cannot
        # request an App reviewer: continue delivers the review to the gate (review_kick).
        from diaktoros import review_kick
        self.as_app_reviewer()
        self.open_pr_row()
        with mock.patch.object(gh, "api", return_value=self.pr(5)), \
                mock.patch.object(review_kick, "kick", return_value=True) as kick:
            ok, lines = continue_run.run(self.loop, self.sup, self.run_id)
        self.assertTrue(ok, lines)
        kick.assert_called_once()
        _loop, pr, head, sender, _tag = kick.call_args.args
        self.assertEqual((pr["number"], head, sender), (5, self.branch_sha, "fix"))
        self.assertTrue(any("delivered the review of PR #5 to the reviewer gate" in line
                            for line in lines), lines)
        self.assertFalse(any(line == "requested review of PR #5" for line in lines), lines)

    def undelivered(self, api, kick, why):
        # A failure path of the delivery: the PR cannot be read, or the delivery fails.
        from diaktoros import review_kick
        self.as_app_reviewer()
        self.open_pr_row()
        effect = {"side_effect": kick} if isinstance(kick, Exception) else {"return_value": kick}
        with mock.patch.object(gh, "api", return_value=api), \
                mock.patch.object(review_kick, "kick", **effect):
            ok, lines = continue_run.run(self.loop, self.sup, self.run_id)
        self.assertTrue(ok, lines)
        self.assertTrue(any(why in line and "the watchdog sweep will" in line
                            for line in lines if "PR #5" in line), lines)

    def test_an_app_reviewer_is_planned_as_a_delivery_not_a_request(self):
        # #1209: GitHub never records a request for an App, so the plan must not claim one.
        self.as_app_reviewer()
        self.open_pr_row()
        plan = continue_run.inspect(self.loop, self.sup, self.run_id)
        self.assertEqual((plan["stages"], plan["conflicts"]), (["deliver_review"], []))
        self.assertFalse(any("already requested" in fact for fact in plan["facts"]), plan)
        text = continue_run.offer(plan, "widgets", self.run_id)
        self.assertIn("complete deliver_review", text)
        self.assertIn("GitHub App", text)
        self.assertNotIn("request_review", text)

    def test_an_app_reviewer_with_no_pr_yet_plans_open_then_deliver(self):
        self.as_app_reviewer()
        plan = continue_run.inspect(self.loop, self.sup, self.run_id)
        self.assertEqual(plan["stages"], ["open_pr", "deliver_review"])

    def test_a_pat_reviewers_plan_is_still_a_request(self):
        self.open_pr_row()
        plan = continue_run.inspect(self.loop, self.sup, self.run_id)
        self.assertEqual(plan["stages"], ["request_review"])
        self.assertNotIn("GitHub App", continue_run.offer(plan, "widgets", self.run_id))

    def test_an_app_verdict_at_an_older_head_does_not_hide_the_unreviewed_head(self):
        # #1209: a verdict at a previous head is not a review of this one; the gate counts
        # verdicts per head (gate.reviewed_at_head), and so must continue.
        from diaktoros import review_kick
        self.as_app_reviewer()
        self.open_pr_row()
        self.reviews = [self.verdict("e" * 40)]
        with mock.patch.object(gh, "api", return_value=self.pr(5)), \
                mock.patch.object(review_kick, "kick", return_value=True) as kick:
            ok, lines = continue_run.run(self.loop, self.sup, self.run_id)
        self.assertTrue(ok, lines)
        kick.assert_called_once()
        self.assertFalse(any("already given" in line for line in lines), lines)

    def test_a_pat_verdict_at_an_older_head_still_gets_a_request(self):
        self.open_pr_row()
        self.reviews = [self.verdict("e" * 40, state="CHANGES_REQUESTED")]
        ok, lines = continue_run.run(self.loop, self.sup, self.run_id)
        self.assertTrue(ok, lines)
        self.assertEqual(self.writes, ["request_review"])

    def test_a_verdict_at_the_current_head_is_already_given_for_either_reviewer(self):
        from diaktoros import review_kick
        for app in (False, True):
            with self.subTest(app=app):
                if app:
                    self.as_app_reviewer()
                self.open_pr_row()
                self.reviews = [self.verdict(self.branch_sha)]
                self.writes = []
                with mock.patch.object(review_kick, "kick") as kick:
                    ok, lines = continue_run.run(self.loop, self.sup, self.run_id)
                self.assertTrue(ok, lines)
                self.assertEqual(self.writes, [])
                kick.assert_not_called()
                self.assertTrue(any("already given at" in line for line in lines), lines)
                with ledger.connect(self.db) as con:
                    con.execute("UPDATE issue_fixes SET state='opened'")

    def test_a_non_verdict_at_the_current_head_does_not_count_as_given(self):
        # Only an approval or change request is a review (gate.reviewed_at_head). The old check
        # counted any review by the reviewer, so a comment, a dismissed or a pending review at
        # the current head hid a head nobody had judged: continue must still hand it over.
        from diaktoros import review_kick
        for app in (False, True):
            for state in ("COMMENTED", "DISMISSED", "PENDING"):
                with self.subTest(app=app, state=state):
                    if app:
                        self.as_app_reviewer()
                    self.open_pr_row()
                    self.reviews = [self.verdict(self.branch_sha, state=state)]
                    self.writes = []
                    plan = continue_run.inspect(self.loop, self.sup, self.run_id)
                    self.assertEqual(plan["stages"], ["deliver_review" if app else "request_review"])
                    self.assertFalse(any("already" in fact for fact in plan["facts"]), plan)
                    with mock.patch.object(gh, "api", return_value=self.pr(5)), \
                            mock.patch.object(review_kick, "kick", return_value=True) as kick:
                        ok, lines = continue_run.run(self.loop, self.sup, self.run_id)
                    self.assertTrue(ok, lines)
                    self.assertFalse(any("already given" in line for line in lines), lines)
                    # The broker call is the audited no-op for an App; the kick is its delivery.
                    self.assertEqual(self.writes, ["request_review"])
                    self.assertEqual(kick.call_count, 1 if app else 0)
                    with ledger.connect(self.db) as con:
                        con.execute("UPDATE issue_fixes SET state='opened'")

    def test_the_cli_dry_run_names_the_app_handoff(self):
        self.as_app_reviewer()
        self.open_pr_row()
        args = argparse.Namespace(loop="widgets", run=self.run_id, dry_run=True)
        with mock.patch.object(config, "load_id", return_value=self.loop), \
                mock.patch.object(operations, "_ledger_path", return_value=self.db), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(operations.cmd_continue(args), 0)
        self.assertIn("deliver_review", out.getvalue())
        self.assertIn("delivers the review to the reviewer gate", out.getvalue())
        self.assertEqual(self.writes, [])

    def test_an_unreadable_pr_says_the_watchdog_will_deliver_its_review(self):
        self.undelivered(None, None, "could not read it")

    def test_a_failed_delivery_says_the_watchdog_will(self):
        self.undelivered(self.pr(5), False, "was not delivered")

    def test_a_raising_delivery_says_the_watchdog_will(self):
        self.undelivered(self.pr(5), RuntimeError("gateway down"), "was not delivered")

    def test_a_pat_reviewers_open_pr_is_still_requested(self):
        from diaktoros import review_kick
        self.prs = [self.pr(5)]
        with ledger.connect(self.db) as con:
            con.execute("UPDATE issue_fixes SET state='opened',pr_number=5")
        with mock.patch.object(review_kick, "kick") as kick:
            ok, lines = continue_run.run(self.loop, self.sup, self.run_id)
        self.assertTrue(ok)
        kick.assert_not_called()
        self.assertIn("requested review of PR #5", lines)

    def test_nothing_missing_writes_nothing(self):
        self.prs = [self.pr(5, requested_reviewers=[{"login": "review"}])]
        ok, _ = continue_run.run(self.loop, self.sup, self.run_id)
        self.assertTrue(ok)
        self.assertEqual(self.writes, [])

    def test_conflicting_facts_stay_quarantined(self):
        cases = {
            "branch moved": lambda: setattr(self, "branch_sha", "e" * 40),
            "branch gone": lambda: setattr(self, "branch_sha", None),
            "two prs": lambda: setattr(self, "prs", [self.pr(5), self.pr(6)]),
            "pr closed": lambda: setattr(self, "prs", [self.pr(5, state="closed")]),
            "pr head differs": lambda: setattr(self, "prs", [self.pr(5, head={"sha": "f" * 40})]),
            "foreign pr": lambda: setattr(self, "prs", [self.pr(5, user={"login": "mallory"})]),
        }
        for name, arrange in cases.items():
            with self.subTest(name):
                self.branch_sha, self.prs, self.writes = NEW, [], []
                arrange()
                ok, lines = continue_run.run(self.loop, self.sup, self.run_id)
                self.assertFalse(ok)
                self.assertEqual(self.writes, [])
                self.assertIn("stays quarantined", lines[-1])
                self.assertEqual(self.run_state(), "uncertain")
                self.assertEqual(self.fix()["state"], "pushed")

    def live_policy(self, loop, live_issue):
        """Run ``continue`` through the real ``broker.authorize_issue_fix`` (#1051): the
        identities verify, the reader sees ``live_issue``, no dependency holds."""
        with mock.patch.object(broker, "authorize_issue_fix", self.real_authorize), \
                mock.patch.object(broker, "_verify_identities", lambda *a, **k: None), \
                mock.patch.object(gh, "api", lambda *a, **k: live_issue), \
                mock.patch("diaktoros.issue_deps.hold_reason", lambda loop, n: ""):
            return continue_run.run(loop, self.sup, self.run_id)

    real_authorize = staticmethod(broker.authorize_issue_fix)

    def assert_refused(self, ok, lines, why):
        self.assertFalse(ok)
        self.assertEqual(self.writes, [])
        self.assertIn("stays quarantined", lines[-1])
        self.assertIn(why, lines[-1])
        self.assertEqual(self.fix()["state"], "pushed")
        self.assertEqual(self.run_state(), "uncertain")

    def test_continue_writes_only_what_live_policy_authorizes(self):
        """#1051: `continue` re-authorizes like the seat's open_pr: the operator's switch off, the
        maintainer's fix label withdrawn or an author off the allowlist each refuse every write."""
        off = config.normalize({**self.raw, "unattended_fixer_push": False})
        self.assert_refused(*self.live_policy(off, issue()), "not enabled by the host operator")
        self.assert_refused(*self.live_policy(self.loop, issue(labels=("bug",))),
                            "no longer on the issue")
        self.assert_refused(*self.live_policy(self.loop, issue(author="mallory")),
                            "not in triage.authors")
        ok, _ = self.live_policy(self.loop, issue())
        self.assertTrue(ok)
        self.assertEqual(self.writes, ["open_pr", "request_review"])

    def test_continue_does_not_open_a_pr_a_plan_first_issue_owes_a_plan_for(self):
        """#2336: the seat's plan-first rule binds `continue` too: a `plan-first` label or a
        large body with no approved plan refuses the PR; a maintainer's approval lets it open."""
        from diaktoros import fix_hold, state as state_mod
        self.assert_refused(*self.live_policy(self.loop, issue(labels=("agent-fix",
                                                                        "plan-first"))),
                            "needs a plan first")
        big = "x" * (fix_hold.PLAN_BODY_MAX + 1)
        self.assert_refused(*self.live_policy(self.loop, issue(body=big)), "needs a plan first")
        st = state_mod.state_for(self.loop)
        st.fix_ask_set(12, "plan", 100, "the plan")
        st.fix_ask_resolve(12, "/loop approve", "jeremy", approve=True)
        ok, _ = self.live_policy(self.loop, issue(labels=("agent-fix", "plan-first")))
        self.assertTrue(ok)
        self.assertEqual(self.writes, ["open_pr", "request_review"])

    def test_a_plan_first_issue_still_gets_its_review_requested(self):
        """#2336: the plan rule guards opening the PR only; a PR already open (opened past the
        seat's own gate) still gets its review handoff."""
        self.prs = [self.pr(5)]
        ok, _ = self.live_policy(self.loop, issue(labels=("agent-fix", "plan-first")))
        self.assertTrue(ok)
        self.assertEqual(self.writes, ["request_review"])

    def test_dry_run_names_the_policy_refusal(self):
        args = argparse.Namespace(loop="widgets", run=self.run_id, dry_run=True)
        off = config.normalize({**self.raw, "unattended_fixer_push": False})
        out = io.StringIO()
        with mock.patch.object(config, "load_id", return_value=off), \
                mock.patch.object(operations, "_ledger_path", return_value=self.db), \
                mock.patch.object(broker, "authorize_issue_fix", self.real_authorize), \
                contextlib.redirect_stdout(out):
            self.assertEqual(operations.cmd_continue(args), 2)
        self.assertIn("not authorized now", out.getvalue())
        self.assertEqual(self.writes, [])

    def test_nothing_missing_needs_no_authorization(self):
        self.prs = [self.pr(5, requested_reviewers=[{"login": "review"}])]
        ok, _ = continue_run.run(self.loop, self.sup, self.run_id)
        self.assertTrue(ok)
        self.assertEqual(getattr(self, "authorized", 0), 0)

    def test_unread_github_stays_quarantined(self):
        with mock.patch.object(gh, "fetch", lambda *a, **k: (None, "timeout")):
            ok, _ = continue_run.run(self.loop, self.sup, self.run_id)
        self.assertFalse(ok)
        self.assertEqual(self.writes, [])

    def test_failed_write_is_quarantined_again_and_not_repeated(self):
        def boom(loop, **kw):
            self.writes.append("open_pr")
            raise broker.BrokerDenied("lost")
        with mock.patch.object(broker, "open_issue_pr", boom):
            ok, _ = continue_run.run(self.loop, self.sup, self.run_id)
            self.assertFalse(ok)
            self.assertEqual(self.fix()["state"], "uncertain")
            continue_run.run(self.loop, self.sup, self.run_id)   # GitHub unchanged, but...
        self.assertEqual(self.writes, ["open_pr", "open_pr"])    # an explicit second request

    def test_chat_continue_from_a_stranger_is_ignored(self):
        self.assertIsNone(continue_run.from_chat(self.loop, self.sup, "mallory",
                                                 f"continue {self.run_id}"))
        self.assertIsNone(continue_run.from_chat(self.loop, self.sup, "", f"continue {self.run_id}"))
        self.assertEqual(self.writes, [])
        self.assertEqual(self.fix()["state"], "pushed")

    def test_chat_continue_from_a_maintainer_runs(self):
        out = continue_run.from_chat(self.loop, self.sup, "JEREMY", f"continue {self.run_id}")
        self.assertTrue(out[0])
        self.assertEqual(self.writes, ["open_pr", "request_review"])
        self.assertIsNone(continue_run.from_chat(self.loop, self.sup, "jeremy", "continue"))

    def test_cli_entry_point_runs_and_dry_run_writes_nothing(self):
        args = argparse.Namespace(loop="widgets", run=self.run_id, dry_run=True)
        with mock.patch.object(config, "load_id", return_value=self.loop), \
                mock.patch.object(operations, "_ledger_path", return_value=self.db), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(operations.cmd_continue(args), 0)
            self.assertEqual(self.writes, [])
            self.assertIn("open_pr and request_review", out.getvalue())
            args.dry_run = False
            self.assertEqual(operations.cmd_continue(args), 0)
        self.assertEqual(self.writes, ["open_pr", "request_review"])

    def test_notice_offers_continue_for_an_issue_fix(self):
        from diaktoros.run_supervisor import Supervisor
        row = {"repo": REPO, "pr": 12, "seat": "issue_fixer", "head": BASE, "id": self.run_id}
        current = {"state": "uncertain", "error": "x", "detail": "", "retries": 0}
        with mock.patch.object(config, "by_repo", return_value=self.loop):
            text = Supervisor._notice_message(row, current, "issue fix")
        self.assertIn(f"hermes dk continue --loop widgets --run {self.run_id}", text)
        self.assertIn(f"continue {self.run_id}", text)


if __name__ == "__main__":
    unittest.main()
