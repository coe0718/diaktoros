#!/usr/bin/env python3
"""#247: the reviewer files its own issue-tier findings — narrowly, as itself, with lineage.

Live, every approval with findings ended in an "Issues to file" list a person had to copy into
GitHub by hand. Now the reviewer files them through one bounded broker operation:

- **attribution and a narrow write:** filed as the reviewer seat's own login, with a title, a
  body and labels from the loop's triage list, and nothing else; at most three per review;
  signed by the host; a title already filed from the PR is refused, so a retry or a later round
  never files it twice;
- **lineage:** each issue records the PR it came from and its depth. A finding on a PR that
  fixed a filed issue is one deeper, and the issue says a person decides whether it goes back;
- **the chain bound:** issue fixes are always capped per day (`triage.fix_daily_turns`, default
  10), because each opens a new PR and the per-PR verdict cap never limits how many happen.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from review_loop import broker, broker_ipc, config, gh, ledger, prompts, run_supervisor  # noqa: E402
from review_loop.run_supervisor import Supervisor  # noqa: E402

HEAD = "a" * 40
REPO = "acme/widgets"


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.root.chmod(0o700)
        tokens = {}
        for login in ("read", "review", "fix"):
            path = self.root / f"{login}.pat"
            path.write_text("DUMMY_SECRET_" + login)
            tokens[login] = str(path)
        self.loop = {"id": "widgets", "repo": REPO, "base": "main", "cap": 3,
                     "state_dir": str(self.root), "fixers": ["fix"], "reviewers": ["review"],
                     "tokens": tokens, "read_token": "read", "reviewer_seat": "review",
                     "seats": {"reviewer": {"login": "review", "agent": "Critic"},
                               "fixer": {"login": "fix"}},
                     "triage": {"route": "widgets-triage", "labels": ["bug", "P2", "P3"],
                                "max_labels": 3}}
        self.pr = {"number": 7, "state": "open", "draft": False, "user": {"login": "fix"},
                   "head": {"sha": HEAD, "ref": "fix-7", "repo": {"full_name": REPO}},
                   "base": {"ref": "main", "repo": {"full_name": REPO}}}
        self.posts, self.next_issue = [], 300
        self.fail_post = False

        def api(loop, path, method="GET", body=None, login=None):
            if path == "/user":
                return {"login": login, "id": {"read": 1, "review": 2, "fix": 3}[login]}
            if method == "POST" and path == f"/repos/{REPO}/issues":
                self.posts.append((body, login))
                if self.fail_post:
                    return None
                self.next_issue += 1
                return {"number": self.next_issue}
            return self.pr
        for obj, name, value in ((gh, "api", mock.Mock(side_effect=api)),
                                 (config, "by_repo", mock.Mock(side_effect=lambda r: self.loop))):
            patcher = mock.patch.object(obj, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db = self.root / "runs.sqlite"

    def run_row(self, number=7, branch_head=HEAD):
        sup = Supervisor(self.db)
        sup.enqueue(f"{REPO}:{number}:{branch_head}:reviewer", REPO, number, branch_head,
                    "reviewer")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='running', launch_intent=? WHERE pr=? AND "
                        "seat='reviewer'", (time.time(), number))
            return sup, con.execute("SELECT id FROM runs WHERE pr=? AND seat='reviewer'",
                                    (number,)).fetchone()[0]

    def broker(self, run_id, branch="fix-7", role="reviewer"):
        scope = broker_ipc.RunScope(REPO, 7, HEAD, role, branch, run_id, str(self.db))
        server = broker_ipc.RunBroker(self.loop, scope, self.root)
        server.__enter__()
        thread = broker_ipc.serve_in_thread(server)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.close)
        return server

    def send(self, server, **request):
        raw = json.dumps({"operation": "file_issue", **request}).encode() + b"\n"
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(6)
            client.connect(str(server.socket_path))
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            return json.loads(client.recv(16384))


class Filing(Base):
    def test_files_as_the_reviewer_with_title_body_labels_only_and_lineage(self):
        _, run_id = self.run_row()
        server = self.broker(run_id)
        answer = self.send(server, title="Docs drift in observer.md", body="evidence at x:1",
                           labels=["p3", "bug"])
        self.assertEqual(answer, {"ok": True, "result": {"accepted": True, "issue": 301}})
        [(payload, login)] = self.posts
        self.assertEqual(login, "review")                       # the reviewer's own account
        self.assertEqual(set(payload), {"title", "body", "labels"})
        self.assertEqual(payload["labels"], ["P3", "bug"])        # the list's own spelling
        self.assertIn("evidence at x:1", payload["body"])
        self.assertIn("Filed by the review loop's reviewer from PR #7", payload["body"])
        self.assertIn("Lineage depth 1.", payload["body"])
        self.assertIn("Automated by", payload["body"])           # host attribution (#197)
        self.assertFalse(server.completed)       # filing is not the review: still owed
        with Supervisor(self.db)._connect() as con:
            row = con.execute("SELECT state,issue_number,depth FROM filed_issues").fetchone()
        self.assertEqual(tuple(row), ("posted", 301, 1))

    def test_bounds_refuse_before_anything_is_written(self):
        _, run_id = self.run_row()
        server = self.broker(run_id)
        cases = {
            "label not in the list": dict(title="t", body="b", labels=["wontfix"]),
            "too many labels": dict(title="t", body="b", labels=["bug", "P2", "P3", "bug"]),
            "two lines": dict(title="a\nb", body="b", labels=[]),
            "long title": dict(title="x" * 121, body="b", labels=[]),
            "empty body": dict(title="t", body="  ", labels=[]),
            "huge body": dict(title="t", body="x" * (broker.FILED_ISSUE_BODY_MAX + 1),
                              labels=[]),
        }
        for name, request in cases.items():
            with self.subTest(name):
                answer = self.send(server, **request)
                self.assertFalse(answer["ok"], answer)
        extra = self.send(server, title="t", body="b", labels=[], assignees=["me"])
        self.assertFalse(extra["ok"])                              # no other field, ever
        self.assertEqual(self.posts, [])

    def test_a_label_outside_the_list_is_refused_by_the_allowlist(self):
        """#298: the allowlist itself refuses it, not a later error — the message says why."""
        _, run_id = self.run_row()
        answer = self.send(self.broker(run_id), title="t", body="b", labels=["invented"])
        self.assertFalse(answer["ok"])
        self.assertIn("labels must come from the loop's triage list", answer["error"])
        self.assertEqual(self.posts, [])

    def test_the_fix_label_is_never_a_reviewers_even_if_planted_in_the_list(self):
        """#298: a reviewer applying the fix label would hand its own finding to the fixer with
        no person in between. Refused on its own, not because another module keeps it out of
        triage.labels — here it is planted there, as a hand-edited loop file could."""
        self.loop["triage"] = {**self.loop["triage"], "fix_label": "agent-fix",
                               "labels": ["bug", "P3", "agent-fix"]}
        _, run_id = self.run_row()
        server = self.broker(run_id)
        for spelling in ("agent-fix", "AGENT-FIX"):
            with self.subTest(spelling):
                answer = self.send(server, title=f"t {spelling}", body="b",
                                   labels=["P3", spelling])
                self.assertFalse(answer["ok"])
                self.assertIn("maintainer's hand-off", answer["error"])
        self.assertEqual(self.posts, [])
        self.assertTrue(self.send(server, title="ok", body="b", labels=["P3"])["ok"])

    def test_one_title_per_pr_and_three_per_review(self):
        _, run_id = self.run_row()
        server = self.broker(run_id)
        self.assertTrue(self.send(server, title="Same thing", body="b", labels=[])["ok"])
        again = self.send(server, title="  same   THING ", body="b", labels=[])
        self.assertFalse(again["ok"])
        self.assertIn("already filed", again["error"])
        for n in (2, 3):
            self.assertTrue(self.send(server, title=f"t{n}", body="b", labels=[])["ok"])
        over = self.send(server, title="t4", body="b", labels=[])
        self.assertIn("at most 3", over["error"])
        self.assertEqual(len(self.posts), 3)

    def test_only_the_reviewer_may_file(self):
        _, run_id = self.run_row()
        for role in ("fixer", "issue_fixer", "triage"):
            with self.subTest(role=role):
                answer = self.send(self.broker(run_id, role=role), title="t", body="b",
                                   labels=[])
                self.assertFalse(answer["ok"])
        with self.assertRaises(broker.BrokerDenied):
            broker.authorize(self.loop, repo=REPO, number=7, head=HEAD, role="fixer",
                             branch="fix-7", operation="file_issue")
        self.assertEqual(self.posts, [])

    def test_an_unknown_post_is_uncertain_and_never_refiled(self):
        _, run_id = self.run_row()
        server = self.broker(run_id)
        self.fail_post = True
        answer = self.send(server, title="t", body="b", labels=[])
        self.assertIn("outcome is unknown", answer["error"])
        self.fail_post = False
        self.assertIn("already filed", self.send(server, title="t", body="b", labels=[])["error"])
        self.assertEqual(len(self.posts), 1)

    def test_a_finding_on_an_automatic_fix_is_one_deeper_and_says_a_person_decides(self):
        sup, run_id = self.run_row()
        # Issue #301 was filed (depth 1); the PR under review fixes it.
        self.send(self.broker(run_id), title="first", body="b", labels=[])
        self.pr["head"]["ref"] = "review-loop/issue-301"            # the PR fixing #301
        answer = self.send(self.broker(run_id, branch="review-loop/issue-301"),
                           title="second", body="b", labels=[])
        self.assertTrue(answer["ok"], answer)
        body = self.posts[-1][0]["body"]
        self.assertIn("Lineage depth 2.", body)
        self.assertIn("a person decides whether it goes back to the fixer", body)
        self.assertEqual(sup.filed_depth(REPO, 302), 2)
        self.assertIsNone(sup.filed_depth(REPO, 999))               # a person's issue


class ChainBound(unittest.TestCase):
    def test_issue_fixes_are_always_capped_per_day(self):
        self.assertEqual(config.seat_daily_turns({}, "issue_fixer"),
                         config.DEFAULT_FIX_DAILY_TURNS)
        self.assertEqual(config.seat_daily_turns({"triage": {"fix_daily_turns": 4}},
                                                 "issue_fixer"), 4)
        loop = {"triage": {"route": "r", "profile": "p", "authors": ["a"], "labels": ["bug"],
                           "fix_label": "agent-fix", "maintainers": ["m"],
                           "fix_daily_turns": 0}}
        with self.assertRaisesRegex(config.ConfigError, "fix_daily_turns"):
            config.normalize_triage(loop["triage"], loop, "f")


class Prompt(unittest.TestCase):
    def test_the_reviewer_is_told_the_label_list_and_to_file_before_reviewing(self):
        facts = dict(repo=REPO, pr=7, url="u", head=HEAD, cap=3, round=1, reviewer_agent="a",
                     fixer_agent="b")
        text = prompts.render_isolated("reviewer", issue_labels="this list only: `P3`", **facts)
        self.assertIn("File each one yourself, before the review", text)
        self.assertIn("labels from this list only: `P3`", text)
        self.assertIn("**Issues filed**", text)
        from review_loop import trusted_turn
        tools = trusted_turn.tool_instructions("reviewer")
        self.assertIn("broker_client file_issue --title", tools)
        self.assertIn("at most 3 per review", tools)
        self.assertNotIn("file_issue", trusted_turn.tool_instructions("fixer"))


if __name__ == "__main__":
    unittest.main()
