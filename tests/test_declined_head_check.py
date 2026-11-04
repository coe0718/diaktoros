"""#1797: a push the reviewer gate declines still leaves ``diaktoros/review`` posted for its head.

The gate is driven through ``gate_reviewer.main`` with GitHub mocked at its readers and at
``gh.api`` (the one place a status is written); the merge check itself is the real one.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import ci, config, gh, merge_check, ready  # noqa: E402
from scripts import gate_reviewer  # noqa: E402

OWNER = "owner-human"
NEW = "d" * 40
OLD = "c" * 40


class DeclinedHead(fg.Base):
    def setUp(self):
        super().setUp()
        path = config.config_dir() / "one.json"
        data = json.loads(path.read_text())
        path.write_text(json.dumps({**data, "review_only": [OWNER], "merge_check": "on"}))
        self.loop = config.load_id("one")
        self.st = __import__("diaktoros.state", fromlist=["x"]).state_for(self.loop)
        self.live = {**fg.LIVE, "user": {"login": OWNER}, "head": {"sha": NEW, "ref": "w-7"}}
        self.posts = []

    def event(self, action: str, rounds: int, sender: str = OWNER, pr: dict | None = None):
        """Run the gate on one ``pull_request`` event at NEW; ``rounds`` verdicts sit on OLD."""
        live = pr or self.live
        reviews = [fg.verdict(rid) | {"commit_id": OLD} for rid in range(5, 5 + rounds)]
        payload = {"action": action, "number": 7, "pull_request": live,
                   "repository": {"full_name": fg.REPO}, "sender": {"login": sender},
                   "requested_reviewer": {"login": "reviewer"}}

        def api(_loop, path, method="GET", body=None, login=None):
            self.posts.append((path, body))
            return {"context": body["context"]}
        with mock.patch.object(gate_reviewer.sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(gh, "pr", return_value=live), \
                mock.patch.object(gh, "api", side_effect=api), \
                mock.patch.object(gh, "open_prs_read", return_value=([live], "")), \
                mock.patch.object(gh, "reviews_read", return_value=(reviews, "")), \
                mock.patch.object(gh, "fetch", return_value=({
                    "number": 7, "state": "open", "mergeable": True, "mergeable_state": "clean",
                    "draft": False, "head": {"sha": NEW}, "base": {"ref": "main"}}, "")), \
                mock.patch.object(gh, "pr_files_read", return_value=([], "")), \
                mock.patch.object(ci, "read", return_value=ci.CIState(passed=["tests"])), \
                mock.patch.object(ready.gate_failures, "open_for", return_value=[]), \
                mock.patch.object(gate_reviewer.gate, "fetch_reviews", return_value=reviews), \
                mock.patch.object(gate_reviewer.gate, "drain_seat"), \
                mock.patch.object(gate_reviewer.observer, "notify"):
            with self.assertRaises(SystemExit):
                gate_reviewer.main()

    def statuses(self) -> list:
        return [(path, body) for path, body in self.posts if path.endswith(f"/statuses/{NEW}")]

    def test_a_review_only_authors_push_posts_the_check_at_once(self):
        self.event("synchronize", rounds=1)
        [(path, body)] = self.statuses()
        self.assertEqual((body["state"], body["context"]), ("pending", "diaktoros/review"))
        self.assertIn(f"not approved at head {NEW[:7]}", body["description"])
        self.assertEqual(self.st._load(self.st.dir / merge_check.FILE, {})["7"]["head"], NEW)

    def test_a_push_at_the_cap_says_the_cap_is_why(self):
        cap = config.review_only_cap(self.loop)
        self.event("synchronize", rounds=cap)
        [(_path, body)] = self.statuses()
        self.assertEqual(body["state"], "pending")
        self.assertIn("review cap reached", body["description"])

    def test_a_kick_declined_at_the_cap_posts_the_check_naming_the_cap(self):
        cap = config.review_only_cap(self.loop)
        for action in ("review_requested", "ready_for_review"):
            self.posts.clear()
            self.st._save(self.st.dir / merge_check.FILE, {})
            with self.subTest(action=action):
                self.event(action, rounds=cap)
                [(_path, body)] = self.statuses()
                self.assertIn("review cap reached", body["description"])

    def test_a_redelivered_event_at_the_same_head_posts_nothing(self):
        self.event("synchronize", rounds=1)
        self.assertEqual(len(self.statuses()), 1)
        self.posts.clear()
        self.event("synchronize", rounds=1)
        self.event("review_requested", rounds=config.review_only_cap(self.loop))
        self.assertEqual(self.posts, [])

    def test_a_pr_outside_the_loop_gets_no_check(self):
        self.event("synchronize", rounds=1, pr={**self.live, "user": {"login": "stranger"}})
        self.assertEqual(self.posts, [])

    def test_a_stacked_childs_push_posts_the_check_the_sweep_would(self):
        stacked = {**self.live, "base": {"ref": "w-6"}}
        self.event("synchronize", rounds=1, pr=stacked)
        [(_path, body)] = self.statuses()
        self.assertEqual((body["state"], body["context"]), ("pending", "diaktoros/review"))

    def test_a_closed_event_posts_nothing(self):
        with mock.patch.object(gate_reviewer.gate, "reclaim"):
            self.event("closed", rounds=1)
        self.assertEqual(self.posts, [])

    def test_a_check_that_is_off_posts_nothing(self):
        path = config.config_dir() / "one.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "merge_check": "off"}))
        self.event("synchronize", rounds=1)
        self.assertEqual(self.posts, [])

    def test_a_refresh_that_blows_up_never_changes_the_gates_answer(self):
        with mock.patch.object(merge_check, "refresh", side_effect=RuntimeError("boom")):
            self.event("synchronize", rounds=1)       # still exits cleanly, nothing raised
        self.assertEqual(self.posts, [])


if __name__ == "__main__":
    unittest.main()
