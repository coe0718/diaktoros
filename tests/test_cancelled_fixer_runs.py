"""Arbiter on #97: a fixer run cancelled at claim (push not admitted / revoked) is visible and recoverable.

Before this, the row was dead for its head: status and explain did not list it, `retry` refused
it ("nothing to retry"), and a redelivery re-armed it only for the claim to cancel it again. The
admission rules, now explicit:

* an automatic redelivery never upgrades a row's admission (a row admitted while pushes were off
  stays unadmitted, and is not pointlessly re-armed); a revoked row re-arms, and runs once the
  policy is back on;
* an operator `hermes dk retry` re-admits under the policy in force *now*, taking a fresh
  admission snapshot under the push-policy lock; while pushes are off it is refused with the
  command that turns them on.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import cli, config, explain, gate, gh, ledger, run_supervisor, seat_model  # noqa: E402
from diaktoros.run_supervisor import pr_facts  # noqa: E402
from diaktoros.run_supervisor import FIXER_NOT_ADMITTED, FIXER_PUSH_REVOKED, Supervisor, views  # noqa: E402

REPO, HEAD = "acme/widgets", "a" * 40
PR = {"number": 7, "state": "open", "draft": False, "user": {"login": "dev"},
      "head": {"sha": HEAD, "ref": "fix-7"}, "base": {"ref": "main", "sha": "b" * 40}}
REVIEW = {"id": 9, "state": "CHANGES_REQUESTED", "commit_id": HEAD}


class CancelledFixer(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.home.chmod(0o700)
        self.loops = self.home / "review-loops.d"
        self.loops.mkdir()
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                           "DIAKTOROS_CONFIG_DIR": str(self.loops)})
        env.start()
        self.addCleanup(env.stop)
        self.pushes(True)
        runtime = self.home / "diaktoros-runtime.json"
        runtime.write_text("{}")
        runtime.chmod(0o600)
        self.db = self.home / "state" / "diaktoros-runs.sqlite"
        self.sup = Supervisor(self.db, production_config=runtime, hermes_home=self.home)
        self.sup._spawn = lambda: None

    def pushes(self, on: bool):
        loop = {"id": "widgets", "repo": REPO, "base": "main", "cap": 3,
                "fixers": ["dev"], "reviewers": ["reviewer"], "reviewer_seat": "reviewer",
                "seats": {"reviewer": {"profile": "r", "route": "review"},
                          "fixer": {"profile": "f", "route": "fix"}},
                "state_dir": str(self.home / "state"), "tokens": {}, "read_token": "reader",
                "host": "http://127.0.0.1:9", "unattended_fixer_push": on}
        (self.loops / "widgets.json").write_text(json.dumps(loop))

    def claim(self):
        with mock.patch.object(gh, "api", return_value=PR), \
             mock.patch.object(gh, "reviews", return_value=[REVIEW]), \
             mock.patch.object(pr_facts, "effective_reviews", return_value=[REVIEW]), \
             mock.patch.object(gate, "latest_effective_review_at_head", return_value=REVIEW), \
             mock.patch.object(gh, "review_state", return_value="CHANGES_REQUESTED"):
            return self.sup._claim()

    def row(self):
        with ledger.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            return dict(con.execute("SELECT * FROM runs WHERE seat='fixer'").fetchone())

    def cli(self, func, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = func(argparse.Namespace(**kw))
        return code, out.getvalue()

    def visible(self):
        [view] = views.read_only_view(self.db, REPO, 7)
        return views.describe_run(view, "widgets")

    def test_revoked_between_admission_and_claim_is_visible_then_recovers(self):
        # His sequence: admitted while pushes were on, then pushes turned off before the claim.
        self.assertEqual(self.sup.submit("fix-1", REPO, 7, HEAD, "fixer",
                                         require_push_admission=True), "enqueued")
        self.pushes(False)
        self.assertIsNone(self.claim())
        self.assertEqual((self.row()["state"], self.row()["error"]),
                         ("cancelled", FIXER_PUSH_REVOKED))
        line = self.visible()                         # status/explain now show it
        self.assertIn("fixer #7 @ aaaaaaa cancelled — fixer push revoked", line)
        self.assertIn("fixer-push --loop widgets --enable --acknowledge-pr-race", line)
        self.assertIn("hermes dk retry --loop widgets --pr 7 --seat fixer", line)
        # `retry` while pushes are still off: refused, naming the command that fixes it.
        code, out = self.cli(cli.cmd_retry, loop="widgets", pr=7, seat="fixer")
        self.assertEqual(code, 2)
        self.assertIn("unattended fixer pushes are off", out)
        self.assertIn("fixer-push --loop widgets --enable", out)
        self.assertEqual(self.row()["state"], "cancelled")
        # The operator opts in, then `retry`: re-admitted under the policy now, and it runs.
        self.pushes(True)
        with mock.patch.object(gate, "resume_isolated", return_value=True):
            code, out = self.cli(cli.cmd_retry, loop="widgets", pr=7, seat="fixer")
        self.assertEqual(code, 0, out)
        self.assertIn("re-armed (was cancelled: fixer push revoked", out)
        self.assertEqual((self.row()["state"], self.row()["push_admitted"]), ("pending", 1))
        self.assertIsNotNone(self.claim())
        self.assertEqual(self.row()["state"], "claimed")

    def test_a_row_admitted_while_pushes_were_off_is_never_upgraded_by_a_redelivery(self):
        # A legacy/raced row: on the ledger with push_admitted=0.
        self.sup.submit("fix-1", REPO, 7, HEAD, "fixer")
        self.assertEqual(self.row()["push_admitted"], 1)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET push_admitted=0")
        self.assertIsNone(self.claim())
        self.assertEqual((self.row()["state"], self.row()["error"]),
                         ("cancelled", FIXER_NOT_ADMITTED))
        self.assertIn("fixer push not admitted", self.visible())
        # Pushes are on now; a redelivered event still does not upgrade it, and says so.
        outcome = self.sup.submit("fix-2", REPO, 7, HEAD, "fixer", require_push_admission=True)
        self.assertTrue(outcome.startswith("duplicate cancelled"), outcome)
        self.assertIn("hermes dk retry", outcome)
        self.assertEqual((self.row()["state"], self.row()["push_admitted"]), ("cancelled", 0))
        # The operator's retry re-admits under the current policy, with a fresh snapshot.
        self.assertEqual(self.sup.retry(self.row()["id"]), "pending")
        self.assertEqual(self.row()["push_admitted"], 1)
        self.assertIsNotNone(self.claim())

    def test_a_redelivery_does_not_rearm_a_revoked_run_while_pushes_are_still_off(self):
        # #741: FIXER_NOT_ADMITTED was refused a re-arm, FIXER_PUSH_REVOKED fell through to
        # "rearmed" and the claim cancelled it again — a futile cycle reported as progress.
        self.sup.submit("fix-1", REPO, 7, HEAD, "fixer", require_push_admission=True)
        self.pushes(False)
        self.assertIsNone(self.claim())
        self.assertEqual(self.row()["error"], FIXER_PUSH_REVOKED)
        spawn = mock.Mock()
        self.sup._spawn = spawn
        outcome = self.sup.submit("fix-2", REPO, 7, HEAD, "fixer")
        self.assertTrue(outcome.startswith("duplicate cancelled"), outcome)
        self.assertIn("hermes dk retry", outcome)
        spawn.assert_not_called()
        self.assertEqual((self.row()["state"], self.row()["error"]),
                         ("cancelled", FIXER_PUSH_REVOKED))
        self.assertIsNone(self.claim())               # nothing pending to claim
        # Pushes back on: the same redelivery re-arms it, and it runs.
        self.pushes(True)
        self.assertEqual(self.sup.submit("fix-3", REPO, 7, HEAD, "fixer"), "rearmed")
        spawn.assert_called_once()
        self.assertEqual(self.row()["state"], "pending")
        self.assertIsNotNone(self.claim())

    def test_explain_names_it_as_the_blocker(self):
        self.sup.submit("fix-1", REPO, 7, HEAD, "fixer")
        self.pushes(False)
        self.claim()
        report = {"url": "u", "read_at": "t", "state_line": "s", "chain": {"status": "direct"},
                  "budget": "b", "seat": "x", "queue": "q", "inflight": "i", "escalation": "e",
                  "hooks": "h", "sweep": "w", "github": "g", "blockers": [],
                  "next": {"action": "n"}, "head": HEAD}
        with mock.patch.object(explain, "explain_facts", return_value={}), \
             mock.patch.object(explain, "explain", return_value=report):
            code, out = self.cli(cli.cmd_explain, loop="widgets", pr=7)
        self.assertEqual(code, 0)
        self.assertIn("blocked:    isolated fixer turn cancelled at this head — fixer push revoked",
                      out)
        self.assertNotIn("nothing — no guard", out)
        self.assertIn("next:       ", out)
        self.assertIn("--enable --acknowledge-pr-race", out.split("next:", 1)[1])

    def test_retry_never_rearms_a_superseded_cancellation(self):
        # Arbiter on #97: `retry` offered every cancelled row; a head that moved was "re-armed",
        # reported as success, and the claim cancelled it again. Only a push-policy cancellation
        # is the operator's to recover; a new head gets its own turn. The surfaces agree.
        self.sup.submit("rev-1", REPO, 7, HEAD, "reviewer")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='cancelled', "
                        "error='PR head moved before the review started'")
        with mock.patch.object(gate, "resume_isolated") as resume:
            code, out = self.cli(cli.cmd_retry, loop="widgets", pr=7, seat="reviewer")
        self.assertEqual(code, 2, out)
        self.assertIn("nothing to retry", out)
        self.assertNotIn("re-armed", out)
        resume.assert_not_called()
        self.assertEqual(views.read_only_view(self.db, REPO, 7), [])
        with ledger.connect(self.db) as con:
            run_id = con.execute("SELECT id FROM runs").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "a new head gets its own turn"):
            self.sup.retry(run_id)
        self.assertEqual(self.row_of("reviewer")["state"], "cancelled")

    def test_an_unreadable_loop_config_is_a_named_refusal_not_a_crash(self):
        # Arbiter on #97: config.by_repo raises ConfigError (not a ValueError) — e.g. two loop files
        # for one repo — and cmd_retry aborted with a traceback, skipping the other candidates.
        self.sup.submit("fix-1", REPO, 7, HEAD, "fixer")
        self.sup.submit("rev-1", REPO, 7, HEAD, "reviewer")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='failed', error='turn exited with status 3'")
        (self.loops / "widgets-copy.json").write_text(
            (self.loops / "widgets.json").read_text().replace('"widgets"', '"copy"'))
        with mock.patch.object(config, "load_id", return_value={"id": "widgets", "repo": REPO}), \
             mock.patch.object(gate, "resume_isolated", return_value=True):
            code, out = self.cli(cli.cmd_retry, loop="widgets", pr=7, seat=None)
        self.assertEqual(code, 2, out)
        self.assertIn("fixer #7 @ aaaaaaa failed: refused: the loop configuration for "
                      "acme/widgets is unusable (duplicate loop configs", out)
        self.assertIn("reviewer #7 @ aaaaaaa re-armed", out)      # the other candidate still ran

    def test_one_predicate_for_the_view_the_step_and_retry(self):
        # Arbiter on #97: runs_view kept its own SQL copy (LIKE, case-insensitive) of the Python
        # prefix test, so a "Fixer push revoked: …" row was listed with a retry remedy that
        # retry then refused. One definition, keyed off the constants, decides all three.
        from diaktoros.run_supervisor import POLICY_CANCELLATIONS, policy_cancelled, views
        self.sup.submit("fix-1", REPO, 7, HEAD, "fixer")
        with ledger.connect(self.db) as con:
            run_id = con.execute("SELECT id FROM runs").fetchone()[0]
        samples = list(POLICY_CANCELLATIONS) + [
            "Fixer push revoked: unattended fixer pushes were disabled after this run was "
            "admitted — no turn launched", "fixer push something else entirely",
            "PR head moved before the review started"]
        self.pushes(True)
        for error in samples:
            with self.subTest(error=error[:30]):
                with ledger.connect(self.db) as con:
                    con.execute("UPDATE runs SET state='cancelled', error=?, retries=0",
                                (error,))
                listed = views.read_only_view(self.db, REPO, 7)
                self.assertEqual(bool(listed), policy_cancelled(error))
                if listed:
                    self.assertIn("retry --loop widgets --pr 7 --seat fixer",
                                  views.next_step(listed[0], "widgets"))
                    self.assertEqual(self.sup.retry(run_id), "pending")
                else:
                    with self.assertRaises(ValueError):
                        self.sup.retry(run_id)
        # The pre-#97 wording of the not-admitted cancellation, on an existing ledger, still counts.
        self.assertTrue(policy_cancelled(
            "fixer push not admitted: unattended fixer pushes were off when this verdict was "
            "enqueued, and a later opt-in cannot authorize this run — no turn launched; this "
            "head needs a manual fix or a new commit"))

    def test_bare_retry_uses_the_head_the_pr_is_at_after_a_backwards_force_push(self):
        # Arbiter on #97: `retry` took the head of the last-*created* row. Head A, then B, then a
        # force-push back to A: A's old row is re-armed (not a new row) and fails again, so the
        # newest head is A even though B's row was created later.
        a, b = HEAD, "b" * 40
        self.sup.submit("rev-a", REPO, 7, a, "reviewer")
        self.sup.submit("rev-b", REPO, 7, b, "reviewer")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='succeeded', updated=100 WHERE head=?", (b,))
            con.execute("UPDATE runs SET state='failed', error='turn exited with status 3', "
                        "updated=200, created=1 WHERE head=?", (a,))
        [view] = views.read_only_view(self.db, REPO, 7)                     # what status/explain list
        self.assertEqual(view["head"], a)
        with mock.patch.object(gate, "resume_isolated", return_value=True):
            code, out = self.cli(cli.cmd_retry, loop="widgets", pr=7, seat=None)
        self.assertEqual(code, 0, out)
        self.assertIn("reviewer #7 @ aaaaaaa re-armed", out)                # the same run

    def test_the_docs_state_the_retry_rule_this_code_enforces(self):
        root = Path(__file__).resolve().parents[1]
        ops = (root / "docs/operations.md").read_text()
        self.assertNotIn("re-arms the PR's failed,\nwaiting or cancelled runs", ops)
        self.assertNotIn("failed, waiting or cancelled runs at its newest head", " ".join(ops.split()))
        self.assertIn("cancelled by the push policy", " ".join(ops.split()))
        doc = Supervisor.retry.__doc__
        self.assertNotIn("a failed, waiting or cancelled run", " ".join(doc.split()))
        self.assertIn("push policy", " ".join(doc.split()))

    def row_of(self, seat):
        with ledger.connect(self.db) as con:
            con.row_factory = sqlite3.Row
            return dict(con.execute("SELECT * FROM runs WHERE seat=?", (seat,)).fetchone())

    def test_an_issue_fix_cancellation_names_its_own_seat_and_the_issue_fix_policy(self):
        # #807: the worker cancels an issue_fixer row with FIXER_PUSH_REVOKED when issue fixes go
        # off; the printed remedy was `retry --seat fixer`, which filters that row out.
        self.sup.submit("fix-1", REPO, 7, HEAD, "issue_fixer")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='cancelled', error=? WHERE seat='issue_fixer'",
                        (FIXER_PUSH_REVOKED,))
        [view] = views.read_only_view(self.db, REPO, 7)
        self.assertEqual(view["seat"], "issue_fixer")
        line = views.describe_run(view, "widgets")
        self.assertIn("hermes dk retry --loop widgets --pr 7 --seat issue_fixer", line)
        self.assertNotIn("--seat fixer", line)
        self.assertIn("issue fixes", line)
        self.assertIn("--fix-label", line)
        # The command it prints is one `retry` can act on: it finds the row.
        code, out = self.cli(cli.cmd_retry, loop="widgets", pr=7, seat="issue_fixer")
        self.assertNotIn("no isolated run on record", out)

    def issue_loop(self, *, triage=True, label=True, push=True):
        """A loop as the worker reads it, with each ``issue_fixes_enabled`` condition switchable."""
        return {"id": "widgets", "repo": REPO, "base": "main", "unattended_fixer_push": push,
                "triage": {**({"route": "t"} if triage else {}),
                           **({"fix_label": "fix-me"} if label else {})}}

    def issue_fix_run_id(self):
        self.sup.submit("fix-1", REPO, 7, HEAD, "issue_fixer")
        with ledger.connect(self.db) as con:
            return con.execute("SELECT id FROM runs WHERE seat='issue_fixer'").fetchone()[0]

    def test_the_worker_names_the_issue_fix_condition_that_failed(self):
        # #980: every failure used to be recorded as "fixer push revoked", even with pushes on.
        cases = ((dict(triage=False), "triage is off"),
                 (dict(label=False), "no fix label is set"),
                 (dict(push=False), "unattended fixer pushes are off"),
                 (dict(triage=False, push=False), "triage is off and unattended fixer pushes are off"))
        for kwargs, cause in cases:
            with self.subTest(cause=cause):
                with ledger.connect(self.db) as con:
                    con.execute("DELETE FROM runs")
                run_id = self.issue_fix_run_id()
                with ledger.connect(self.db) as con:
                    con.execute("UPDATE runs SET state='launching', owner='w', launch_intent=1, "
                                "generation=COALESCE(NULLIF(generation, ''), 'g1')")
                with mock.patch.object(config, "by_repo", return_value=self.issue_loop(**kwargs)), \
                        mock.patch.object(seat_model, "load_runtime",
                                          return_value={}), \
                        mock.patch.object(self.sup, "recover"):
                    self.sup._run_production(run_id, "w")
                row = self.row_of("issue_fixer")
                self.assertEqual(row["state"], "cancelled")
                self.assertIn(cause, row["error"])
                self.assertNotIn("fixer push revoked", row["error"])
                if kwargs == dict(triage=False):
                    self.assertNotIn("fix label", row["error"])
                    self.assertNotIn("fixer pushes are off", row["error"])
                # The cancellation is one the view lists and `retry` recovers, remedy included.
                self.assertTrue(run_supervisor.policy_cancelled(row["error"]))
                [view] = views.read_only_view(self.db, REPO, 7)
                self.assertIn(cause, views.describe_run(view, "widgets"))
                self.assertIn("--seat issue_fixer", views.describe_run(view, "widgets"))

    def test_retry_of_an_issue_fix_is_refused_while_issue_fixes_are_off(self):
        run_id = self.issue_fix_run_id()
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='cancelled', error=? WHERE id=?",
                        (run_supervisor.issue_fix_off_reason(self.issue_loop(label=False))
                         + run_supervisor.ISSUE_FIX_LAUNCH_SUFFIX, run_id))
        # Pushes are on (self.pushes(True)), but triage has no fix label: still refused, with
        # the failed condition; the row stays cancelled.
        with mock.patch.object(config, "by_repo", return_value=self.issue_loop(label=False)):
            with self.assertRaises(ValueError) as caught:
                self.sup.retry(run_id)
            code, out = self.cli(cli.cmd_retry, loop="widgets", pr=7, seat="issue_fixer")
        self.assertIn("issue fixes are off: no fix label is set", str(caught.exception))
        self.assertEqual(code, 2)
        self.assertIn("issue fixes are off: no fix label is set", out)
        self.assertEqual(self.row_of("issue_fixer")["state"], "cancelled")
        # Once they are on, the same retry re-arms it.
        with mock.patch.object(config, "by_repo", return_value=self.issue_loop()):
            self.assertEqual(self.sup.retry(run_id), "pending")

    def test_a_superseded_cancellation_stays_out_of_the_view(self):
        self.sup.submit("fix-1", REPO, 7, HEAD, "fixer")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='cancelled', error='fixer verdict superseded'")
        self.assertEqual(views.read_only_view(self.db, REPO, 7), [])


if __name__ == "__main__":
    unittest.main()
