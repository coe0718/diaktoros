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

    def real_policy(self, live_issue):
        """The real ``broker.authorize_issue_fix`` (#1051) with ``live_issue`` as the reader's
        read: the identities verify, no dependency holds."""
        stack = contextlib.ExitStack()
        for patch in (mock.patch.object(broker, "authorize_issue_fix", self.real_authorize),
                      mock.patch.object(broker, "_verify_identities", lambda *a, **k: None),
                      mock.patch.object(gh, "api", lambda *a, **k: live_issue),
                      mock.patch("diaktoros.issue_deps.hold_reason", lambda loop, n: "")):
            stack.enter_context(patch)
        return stack

    def live_policy(self, loop, live_issue):
        """Run ``continue`` through the real ``broker.authorize_issue_fix`` (#1051): the
        identities verify, the reader sees ``live_issue``, no dependency holds."""
        with self.real_policy(live_issue):
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

    def test_continue_applies_the_plan_first_gate_of_the_seats_open_pr(self):
        """#2334: a pushed fix of a plan-first issue (the label, or a body over PLAN_BODY_MAX)
        gets no PR from `continue` with no approved plan (none recorded locally either), as the
        broker's own open_pr refuses it (fix_hold.plan_required); the dry run names the way out."""
        from diaktoros import fix_hold
        labelled = issue(labels=("agent-fix", fix_hold.PLAN_LABEL))
        large = issue(body="x" * (fix_hold.PLAN_BODY_MAX + 1))
        for live in (labelled, large):
            with self.subTest(labels=live["labels"], body=len(live["body"])), \
                    mock.patch.object(gh, "fetch", self.fetch_with(live)):
                with self.real_policy(live):
                    plan = continue_run.inspect(self.loop, self.sup, self.run_id)
                self.assertEqual(plan["stages"], [])
                said = continue_run.offer(plan, "widgets", self.run_id)
                self.assertIn("needs a plan first", said)
                self.assertIn("--post-plan", said)
                self.assert_refused(*self.live_policy(self.loop, live), "needs a plan first")

    def test_the_plan_rule_reads_the_issue_that_authorized_the_fix(self):
        """#2334 review F2: the label lands between inspect's issue read and the authorizing
        read. The plan rule is applied to the authorizing (newer) read, so nothing is opened."""
        from diaktoros import fix_hold
        labelled = issue(labels=("agent-fix", fix_hold.PLAN_LABEL))
        with mock.patch.object(gh, "fetch", self.fetch_with(issue())):    # inspect's older read
            self.assert_refused(*self.live_policy(self.loop, labelled), "needs a plan first")

    def plan_comment(self, loop, *, repo, number, login, body):
        self.writes.append("plan_comment")
        self.posted = body
        return 500

    def approve_after_plan(self, labelled, comments):
        """``continue --post-plan`` through the CLI for a refused plan-first run, then the
        maintainer's ``comments`` on the issue as GitHub lists them."""
        self.comments = comments
        args = argparse.Namespace(loop="widgets", run=self.run_id, dry_run=False, post_plan=True)
        with mock.patch.object(gh, "fetch", self.fetch_with(labelled)), self.real_policy(labelled), \
                mock.patch.object(broker, "post_issue_comment", self.plan_comment), \
                mock.patch.object(config, "load_id", return_value=self.loop), \
                mock.patch.object(operations, "_ledger_path", return_value=self.db), \
                contextlib.redirect_stdout(io.StringIO()):
            return operations.cmd_continue(args)

    def test_a_plan_first_fix_recovers_through_a_posted_plan_and_a_real_approve(self):
        """#2334 review F1: `--post-plan` posts the pushed branch as the plan (one fixer comment
        ending in the /loop approve line) and opens a plan ask; the watchdog sweep reads a
        maintainer's `/loop approve` after it from the issue's comments, records the approval
        and queues no new turn (the branch is pushed); `continue` then opens the PR."""
        from diaktoros import fix_hold, gate, state as state_mod
        st = state_mod.state_for(self.loop)
        labelled = issue(labels=("agent-fix", fix_hold.PLAN_LABEL))
        self.assertEqual(self.approve_after_plan(labelled, []), 0)
        self.assertEqual(self.writes, ["plan_comment"])
        self.assertIn(BRANCH, self.posted)
        self.assertIn(f"exactly `{fix_hold.APPROVE}`", self.posted)
        self.assertEqual(st.fix_ask_open()[12]["run"], self.run_id)
        self.assertEqual(self.approve_after_plan(labelled, []), 2)   # an ask is open: no 2nd post
        self.assertEqual(self.writes, ["plan_comment"])
        self.writes = []
        queued = []
        comments = [{"id": 499, "user": {"login": "jeremy"}, "body": "/loop approve"},  # before
                    {"id": 501, "user": {"login": "owner"}, "body": "/loop approve"}]  # not maint.
        with mock.patch.object(gh, "issue_comments_read", lambda loop, n: (comments, "")), \
                mock.patch.object(gate, "enqueue_isolated", lambda *a, **k: queued.append(a)):
            fix_hold.sweep_asks(self.loop, st, [])
            self.assertFalse(st.fix_plan_approved(12))
            self.assert_refused(*self.live_policy(self.loop, labelled), "needs a plan first")
            comments.append({"id": 502, "user": {"login": "Jeremy"}, "body": "/loop approve"})
            fix_hold.sweep_asks(self.loop, st, [])
        self.assertTrue(st.fix_plan_approved(12))
        self.assertEqual((st.fix_ask_open(), queued), ({}, []))
        with mock.patch.object(gh, "fetch", self.fetch_with(labelled)):
            ok, lines = self.live_policy(self.loop, labelled)
        self.assertTrue(ok, lines)
        self.assertEqual(self.writes, ["open_pr", "request_review"])

    def test_post_plan_refuses_a_run_that_needs_no_plan(self):
        self.assertEqual(self.approve_after_plan(issue(), []), 2)
        self.assertEqual(self.writes, [])

    def test_competing_post_plans_post_one_plan(self):
        """#2334 review F3: a second `--post-plan` that starts while the first is posting (its
        check already passed in the old code) waits for the lock and then finds the issue's one
        ask taken: one comment, one ask, counted once."""
        import threading
        from diaktoros import fix_hold, state as state_mod
        st = state_mod.state_for(self.loop)
        labelled = issue(labels=("agent-fix", fix_hold.PLAN_LABEL))
        results, second = [], []

        def compete(loop, **kw):
            if not second:              # the first caller is mid-post: start the second one
                second.append(threading.Thread(target=lambda: results.append(
                    continue_run.post_plan(self.loop, self.sup, self.run_id)[0])))
                second[0].start()
                second[0].join(1)       # it must wait for the first: it may not post meanwhile
            return self.plan_comment(loop, **kw)
        with mock.patch.object(gh, "fetch", self.fetch_with(labelled)), self.real_policy(labelled), \
                mock.patch.object(broker, "post_issue_comment", compete):
            first, _ = continue_run.post_plan(self.loop, self.sup, self.run_id)
            second[0].join(10)
        self.assertEqual((first, results), (True, [False]))
        self.assertEqual(self.writes, ["plan_comment"])
        self.assertEqual(st.fix_ask_count(12), 1)
        self.assertEqual(st.fix_ask_open()[12]["comment_id"], 500)

    def post_plan_with(self, labelled, comments, post=None):
        """``--post-plan`` with the issue's comments as GitHub lists them, and the POST replaced
        by ``post`` when given."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(gh, "issue_comments_read",
                                              lambda loop, n: (comments, "")))
        if post:
            stack.enter_context(mock.patch.object(Continue, "plan_comment", post))
        with stack:
            return self.approve_after_plan(labelled, comments)

    def test_an_unrecorded_plan_comment_is_adopted_not_posted_again(self):
        """#2334 review F4: the plan comment posts but recording its ask fails. The reservation
        stays open (`pending`), so no other ask can start and no `/loop approve` resolves it;
        `--post-plan` again finds the run's comment on the issue and adopts it: no 2nd post."""
        from diaktoros import continue_run, fix_hold, gate, state as state_mod
        st = state_mod.state_for(self.loop)
        labelled = issue(labels=("agent-fix", fix_hold.PLAN_LABEL))

        def disk_full(state, path, data):
            ask = ((data.get("open") or {}).get("12") or {}) if isinstance(data, dict) else {}
            if path.name == "fix-asks.json" and type(ask.get("comment_id")) is int:
                raise OSError("disk full")              # the write that records the comment
            return real_save(state, path, data)
        real_save = state_mod.LoopState._save_strict
        with mock.patch.object(state_mod.LoopState, "_save_strict", disk_full):
            self.assertEqual(self.post_plan_with(labelled, []), 2)
        self.assertEqual(self.writes, ["plan_comment"])
        self.assertTrue(st.fix_ask_open()[12]["pending"])
        self.assertRaises(ValueError, fix_hold.check_ask, st, 12)      # the seat is held off too
        on_issue = [{"id": 500, "user": {"login": "fix"}, "body": self.posted},
                    {"id": 501, "user": {"login": "Jeremy"}, "body": "/loop approve"}]
        with mock.patch.object(gh, "issue_comments_read", lambda loop, n: (on_issue, "")), \
                mock.patch.object(gate, "enqueue_isolated", lambda *a, **k: None):
            fix_hold.sweep_asks(self.loop, st, [])
        self.assertFalse(st.fix_plan_approved(12))                    # unconfirmed: not resolved
        self.assertTrue(self.posted.startswith(continue_run.plan_marker(self.run_id)))
        self.assertEqual(self.post_plan_with(labelled, on_issue[:1]), 0)
        self.assertEqual(self.writes, ["plan_comment"])                  # adopted, not reposted
        self.assertEqual(st.fix_ask_open()[12]["comment_id"], 500)
        self.assertNotIn("pending", st.fix_ask_open()[12])
        self.assertEqual(st.fix_ask_count(12), 1)

    def test_a_lost_post_response_is_reconciled_by_the_next_post_plan(self):
        """#2334 review F4: the POST's response is lost. If GitHub has the comment the retry
        adopts it; if not, the retry posts it once. Either way one ask, counted once."""
        from diaktoros import fix_hold, state as state_mod
        st = state_mod.state_for(self.loop)
        labelled = issue(labels=("agent-fix", fix_hold.PLAN_LABEL))

        def lost(s, loop, **kw):
            s.writes.append("plan_comment")
            s.posted = kw["body"]
            raise broker.BrokerDenied("GitHub comment write did not return a successful response")
        for landed in (True, False):
            with self.subTest(landed=landed):
                st.fix_ask_drop(12)
                st.kv_set_strict(st.dir / "fix-asks.json", {})
                self.writes = []
                self.assertEqual(self.post_plan_with(labelled, [], post=lost), 2)
                self.assertTrue(st.fix_ask_open()[12]["pending"])
                on_issue = [{"id": 500, "user": {"login": "fix"}, "body": self.posted}] \
                    if landed else []
                self.assertEqual(self.post_plan_with(labelled, on_issue), 0)
                self.assertEqual(self.writes, ["plan_comment"] * (1 if landed else 2))
                self.assertEqual(st.fix_ask_open()[12]["comment_id"], 500)
                self.assertEqual(st.fix_ask_count(12), 1)

    def lost_plan(self, live):
        """``--post-plan`` on the plan-first issue ``live`` whose POST response is lost: the
        run's reservation is left ``pending``, the comment body in ``self.posted``."""
        from diaktoros import state as state_mod

        def lost(s, loop, **kw):
            s.writes.append("plan_comment")
            s.posted = kw["body"]
            raise broker.BrokerDenied("GitHub comment write did not return a successful response")
        st = state_mod.state_for(self.loop)
        st.kv_set_strict(st.dir / "fix-asks.json", {})
        self.writes = []
        self.assertEqual(self.post_plan_with(live, [], post=lost), 2)
        self.assertTrue(st.fix_ask_open()[12]["pending"])
        return st

    def test_a_pending_plan_is_settled_after_the_plan_rule_goes_away(self):
        """#2381: the POST's response is lost, then the plan-first label is removed or the body
        shortened. `--post-plan` refuses (no plan is needed) but no longer strands the run's
        reservation: with no comment of the run on the issue it is dropped and uncounted, with
        one it is closed; either way the seat may ask again and fix turns are not held."""
        from diaktoros import fix_hold
        for live in (issue(labels=("agent-fix", fix_hold.PLAN_LABEL)),
                     issue(body="x" * (fix_hold.PLAN_BODY_MAX + 1))):
            for landed in (False, True):
                with self.subTest(labels=live["labels"], landed=landed):
                    st = self.lost_plan(live)
                    on_issue = [{"id": 500, "user": {"login": "fix"}, "body": self.posted}] \
                        if landed else []
                    self.assertEqual(self.post_plan_with(issue(), on_issue), 2)
                    self.assertEqual(st.fix_ask_open(), {})
                    self.assertEqual(st.fix_ask_count(12), 1 if landed else 0)
                    fix_hold.check_ask(st, 12)                 # the seat may ask again
                    self.assertEqual(self.writes, ["plan_comment"])   # nothing posted again

    def test_a_pending_plan_is_adopted_after_the_branch_moves(self):
        """#2381: the POST's response is lost, then the branch moves (a conflict). `--post-plan`
        refuses, but adopts the run's comment if GitHub has it, so a maintainer's approve
        resolves it, and drops the reservation (uncounted) if it does not."""
        from diaktoros import fix_hold
        labelled = issue(labels=("agent-fix", fix_hold.PLAN_LABEL))
        for landed in (True, False):
            with self.subTest(landed=landed):
                self.branch_sha = NEW
                st = self.lost_plan(labelled)
                self.branch_sha = "e" * 40
                on_issue = [{"id": 500, "user": {"login": "fix"}, "body": self.posted}] \
                    if landed else []
                self.assertEqual(self.post_plan_with(labelled, on_issue), 2)
                if landed:
                    self.assertEqual(st.fix_ask_open()[12]["comment_id"], 500)
                    self.assertNotIn("pending", st.fix_ask_open()[12])
                    self.assertEqual(st.fix_ask_count(12), 1)
                else:
                    self.assertEqual((st.fix_ask_open(), st.fix_ask_count(12)), ({}, 0))
                self.assertEqual(self.writes, ["plan_comment"])

    def test_continue_closes_the_runs_plan_ask_when_it_opens_the_pr(self):
        """#2381: a pending plan ask of the run, then the label is removed: `continue` opens the
        PR and closes the run's ask, so the issue is not left waiting for a maintainer."""
        from diaktoros import fix_hold
        st = self.lost_plan(issue(labels=("agent-fix", fix_hold.PLAN_LABEL)))
        args = argparse.Namespace(loop="widgets", run=self.run_id, dry_run=False, post_plan=False)
        with mock.patch.object(gh, "fetch", self.fetch_with(issue())), self.real_policy(issue()), \
                mock.patch.object(gh, "issue_comments_read", lambda loop, n: ([], "")), \
                mock.patch.object(config, "load_id", return_value=self.loop), \
                mock.patch.object(operations, "_ledger_path", return_value=self.db), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(operations.cmd_continue(args), 0)
        self.assertEqual(self.writes, ["plan_comment", "open_pr", "request_review"])
        self.assertEqual(st.fix_ask_open(), {})
        fix_hold.check_ask(st, 12)

    def post_plan_unreadable(self, live):
        """``--post-plan`` with the issue read as ``live`` and its comments unreadable."""
        with mock.patch.object(gh, "issue_comments_read", lambda loop, n: (None, "HTTP 502")):
            return self.approve_after_plan(live, None)

    def test_a_plan_rule_gone_with_another_conflict_still_settles_the_ask(self):
        """#2382 review F2: the label is removed AND the branch moves. The branch conflict does
        not hide that no plan is needed any more: the run's ask is closed, pending (comment
        landed or not) or confirmed, and the seat may ask again."""
        from diaktoros import fix_hold
        labelled = issue(labels=("agent-fix", fix_hold.PLAN_LABEL))
        for case in ("pending-landed", "pending-not-landed", "confirmed"):
            with self.subTest(case=case):
                self.branch_sha = NEW
                if case == "confirmed":
                    st = self.lost_plan(labelled)
                    self.assertEqual(self.post_plan_with(labelled, [], post=Continue.plan_comment), 0)
                    self.assertNotIn("pending", st.fix_ask_open()[12])
                    self.writes = ["plan_comment"]
                else:
                    st = self.lost_plan(labelled)
                landed = case != "pending-not-landed"
                on_issue = [{"id": 500, "user": {"login": "fix"}, "body": self.posted}] \
                    if landed else []
                self.branch_sha = "e" * 40
                self.assertEqual(self.post_plan_with(issue(), on_issue), 2)
                self.assertEqual(st.fix_ask_open(), {})
                self.assertEqual(st.fix_ask_count(12), 1 if landed else 0)
                fix_hold.check_ask(st, 12)

    def test_unreadable_comments_and_confirmed_asks_are_settled_as_documented(self):
        """#2382 review F4: (a) no conflict, plan no longer needed, comments unreadable: the
        pending ask is closed but stays counted (its comment may be on the issue); (b) the same
        with a branch conflict while the issue still needs a plan: it stays pending for a retry;
        (c) a confirmed ask of the run, plan no longer needed: closed, still counted."""
        from diaktoros import fix_hold
        labelled = issue(labels=("agent-fix", fix_hold.PLAN_LABEL))
        st = self.lost_plan(labelled)
        self.assertEqual(self.post_plan_unreadable(issue()), 2)                     # (a)
        self.assertEqual((st.fix_ask_open(), st.fix_ask_count(12)), ({}, 1))
        st = self.lost_plan(labelled)
        self.branch_sha = "e" * 40
        self.assertEqual(self.post_plan_unreadable(labelled), 2)                    # (b)
        self.assertTrue(st.fix_ask_open()[12]["pending"])
        self.assertEqual(st.fix_ask_count(12), 1)
        self.branch_sha = NEW
        self.assertEqual(self.post_plan_with(labelled, [], post=Continue.plan_comment), 0)
        self.assertEqual(st.fix_ask_open()[12]["comment_id"], 500)
        self.assertEqual(self.post_plan_unreadable(issue()), 2)                     # (c)
        self.assertEqual((st.fix_ask_open(), st.fix_ask_count(12)), ({}, 1))
        self.assertEqual(self.writes, ["plan_comment", "plan_comment"])

    def test_release_never_touches_another_runs_ask(self):
        """#2382 review F4: a pending or confirmed plan ask of another run, and the seat's own
        question, are left as they are by this run's release, count included."""
        from diaktoros import state as state_mod
        st = state_mod.state_for(self.loop)
        for ask in ({"kind": "plan", "comment_id": None, "text": "t", "run": "other", "pending": True},
                    {"kind": "plan", "comment_id": 9, "text": "t", "run": "other"},
                    {"kind": "question", "comment_id": 9, "text": "t"}):
            with self.subTest(ask=ask):
                st.kv_set_strict(st.dir / "fix-asks.json", {"open": {"12": ask}, "count": {"12": 1}})
                for keep in (True, False):
                    self.assertEqual(continue_run._release_plan_ask(
                        self.loop, st, 12, self.run_id, keep_adopted=keep), "")
                self.assertEqual((st.fix_ask_open()[12], st.fix_ask_count(12)), (ask, 1))

    def test_continue_closes_the_plan_ask_even_when_the_review_request_fails(self):
        """#2382 review F3: the PR is created, then the review request fails. The PR is open, so
        the run's plan ask is closed then, not only after the review handoff."""
        from diaktoros import fix_hold
        st = self.lost_plan(issue(labels=("agent-fix", fix_hold.PLAN_LABEL)))

        def refused(loop, **kw):
            self.writes.append("request_review")
            raise broker.BrokerDenied("review request failed")
        args = argparse.Namespace(loop="widgets", run=self.run_id, dry_run=False, post_plan=False)
        with mock.patch.object(gh, "fetch", self.fetch_with(issue())), self.real_policy(issue()), \
                mock.patch.object(broker, "request_issue_pr_review", refused), \
                mock.patch.object(gh, "issue_comments_read", lambda loop, n: ([], "")), \
                mock.patch.object(config, "load_id", return_value=self.loop), \
                mock.patch.object(operations, "_ledger_path", return_value=self.db), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(operations.cmd_continue(args), 2)
        self.assertEqual(self.writes, ["plan_comment", "open_pr", "request_review"])
        self.assertEqual((st.fix_ask_open(), st.fix_ask_count(12)), ({}, 0))

    def test_the_sweep_never_resolves_an_ask_that_opened_while_it_read_replies(self):
        """#2382 review F1: the sweep snapshots the run's confirmed plan ask and reads its
        replies; meanwhile `continue` closes that ask and the seat opens a new question. The
        old plan's `/loop approve` must not approve or close the new question."""
        from diaktoros import fix_hold, gate
        labelled = issue(labels=("agent-fix", fix_hold.PLAN_LABEL))
        st = self.lost_plan(labelled)
        self.assertEqual(self.post_plan_with(labelled, [], post=Continue.plan_comment), 0)
        self.assertEqual(st.fix_ask_open()[12]["comment_id"], 500)

        def interleaved(loop, n):
            continue_run._release_plan_ask(self.loop, st, 12, self.run_id, keep_adopted=False)
            st.fix_ask_set(12, "question", 600, "new question")
            return [{"id": 601, "user": {"login": "jeremy"}, "body": "/loop approve"}], ""
        queued = []
        with mock.patch.object(gh, "issue_comments_read", interleaved), \
                mock.patch.object(gate, "enqueue_isolated", lambda *a, **k: queued.append(a)):
            fix_hold.sweep_asks(self.loop, st, [])
        self.assertEqual(st.fix_ask_open()[12]["comment_id"], 600)
        self.assertFalse(st.fix_plan_approved(12))
        self.assertIsNone(st.fix_handoff(12))
        self.assertEqual(queued, [])

    def test_continue_leaves_another_runs_ask_alone(self):
        """#2381: only the run's own ask is released; a seat's open question stays."""
        from diaktoros import state as state_mod
        st = state_mod.state_for(self.loop)
        st.fix_ask_set(12, "question", 400, "which way?")
        with mock.patch.object(gh, "fetch", self.fetch_with(issue())), self.real_policy(issue()):
            continue_run.run(self.loop, self.sup, self.run_id)
            continue_run.post_plan(self.loop, self.sup, self.run_id)
        self.assertEqual(st.fix_ask_open()[12]["comment_id"], 400)

    def fetch_with(self, live):
        """``fetch`` with the issue read as ``live``; the branch and PRs as in ``fetch``."""
        def fetch(loop, path, method="GET", body=None, login=None):
            if "/git/ref/heads/" in path or "/pulls?" in path:
                return Continue.fetch(self, loop, path, method, body, login)
            return live, ""
        return fetch

    def test_an_unreadable_issue_is_not_checked_rather_than_refused(self):
        """#2335: GitHub unreachable during the live check keeps the run quarantined, but says
        the fix could not be checked now, not that policy refused it."""
        ok, lines = self.live_policy(self.loop, None)
        self.assert_refused(ok, lines, "could not be checked now: issue unreadable")
        self.assertNotIn("not authorized now", lines[-1])

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
