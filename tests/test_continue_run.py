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
                (safe_push, mock.patch.object(safe_push, "open_branch", self.no_push))):
            fake.start()
            self.addCleanup(fake.stop)

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
