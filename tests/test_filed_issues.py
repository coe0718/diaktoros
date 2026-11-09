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
import contextlib
import io
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
from diaktoros import (broker, broker_filing, broker_client, broker_ipc, config, gh, ledger, prompts,  # noqa: E402
                       run_supervisor)
from diaktoros.config import blocks  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

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
        self.assertIn("Filed by the Diaktoros reviewer from PR #7", payload["body"])
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
            "two lines": dict(title="a\nb", body="b", labels=["bug"]),
            "long title": dict(title="x" * 121, body="b", labels=["bug"]),
            "empty body": dict(title="t", body="  ", labels=["bug"]),
            "huge body": dict(title="t", body="x" * (broker_filing.FILED_ISSUE_BODY_MAX + 1),
                              labels=["bug"]),
        }
        for name, request in cases.items():
            with self.subTest(name):
                answer = self.send(server, **request)
                self.assertFalse(answer["ok"], answer)
        extra = self.send(server, title="t", body="b", labels=["bug"], assignees=["me"])
        self.assertFalse(extra["ok"])                              # no other field, ever
        self.assertEqual(self.posts, [])

    def test_a_label_outside_the_list_is_refused_by_the_allowlist(self):
        """#298: the allowlist itself refuses it, not a later error — the message says why."""
        _, run_id = self.run_row()
        answer = self.send(self.broker(run_id), title="t", body="b", labels=["invented"])
        self.assertFalse(answer["ok"])
        self.assertIn("labels must come from the loop's triage list", answer["error"])
        self.assertEqual(self.posts, [])

    def test_a_priority_only_issue_is_refused_and_a_type_plus_priority_is_filed(self):
        """#967: a maintainer selecting by type must find every filed issue."""
        _, run_id = self.run_row()
        server = self.broker(run_id)
        for labels in (["P3"], ["p2", "P3"], []):
            with self.subTest(labels=labels):
                answer = self.send(server, title="no type", body="b", labels=labels)
                self.assertFalse(answer["ok"], answer)
                self.assertIn("needs a type label", answer["error"])
                self.assertIn("send a type label and a priority label", answer["error"])
                self.assertIn("nothing was filed", answer["error"])
        self.assertEqual(self.posts, [])
        with Supervisor(self.db)._connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM filed_issues").fetchone()[0], 0)
        answer = self.send(server, title="no type", body="b", labels=["bug", "P3"])
        self.assertTrue(answer["ok"], answer)         # the same title: the refusals spent nothing
        self.assertEqual(self.posts[0][0]["labels"], ["bug", "P3"])

    def test_a_cap_of_one_refuses_the_pair_and_the_message_says_what_to_drop(self):
        """The issue: with max_labels 1 the prompt's type-plus-priority pair can't be filed.
        The cap is the broker's rule, so the type label alone is accepted and the pair is
        refused with a message that says so, not as a bad label."""
        self.loop["triage"] = {**self.loop["triage"], "labels": ["bug", "P3"], "max_labels": 1}
        _, run_id = self.run_row()
        server = self.broker(run_id)
        answer = self.send(server, title="pair", body="b", labels=["bug", "P3"])
        self.assertFalse(answer["ok"], answer)
        self.assertIn("at most 1 labels per issue", answer["error"])
        self.assertIn("send the type label alone", answer["error"])
        self.assertEqual(self.posts, [])
        # A priority alone is refused too, and its advice must not tell the reviewer to add
        # a priority beside the type (which the cap refuses): the two messages agree.
        answer = self.send(server, title="pair", body="b", labels=["P3"])
        self.assertFalse(answer["ok"], answer)
        self.assertIn("needs a type label", answer["error"])
        self.assertIn("send the type label alone", answer["error"])
        self.assertNotIn("as well as a priority", answer["error"])
        self.assertTrue(self.send(server, title="pair", body="b", labels=["bug"])["ok"])
        self.assertEqual(self.posts[0][0]["labels"], ["bug"])

    def test_a_cap_of_one_advice_follows_the_kinds_the_list_has(self):
        """F1 of #1741: with a cap of 1, a priorities-only list is told to send a priority, a
        types-only list a type, and no list nothing; the broker accepts exactly what the
        over-cap refusal asks for, and the prompt's label line says the same."""
        from diaktoros.run_supervisor import turn_prompts
        cases = [(["P2", "P3"], ["P2", "P3"], "send a priority label", ["P3"]),
                 (["bug", "enhancement"], ["bug", "enhancement"], "send a type label", ["bug"]),
                 (["bug", "P3"], ["bug", "P3"], "send the type label alone", ["bug"])]
        _, run_id = self.run_row()
        server = self.broker(run_id)
        for allowed, pair, advice, good in cases:
            with self.subTest(labels=allowed):
                self.loop["triage"] = {**self.loop["triage"], "labels": allowed, "max_labels": 1}
                answer = self.send(server, title="t" + "-".join(allowed), body="b", labels=pair)
                self.assertFalse(answer["ok"], answer)
                self.assertIn(advice, answer["error"])
                if "type" not in advice:
                    self.assertNotIn("type label", answer["error"])
                self.assertIn(advice, turn_prompts.issue_labels_fact(self.loop))
                self.assertTrue(self.send(server, title="t" + "-".join(allowed), body="b", labels=good)["ok"])
        self.loop["triage"] = {**self.loop["triage"], "labels": [], "max_labels": 1}
        self.assertIn("file without labels", turn_prompts.issue_labels_fact(self.loop))

    def test_a_list_with_no_type_label_cannot_be_asked_for_one(self):
        self.loop["triage"] = {**self.loop["triage"], "labels": ["P2", "P3"]}
        _, run_id = self.run_row()
        self.assertTrue(self.send(self.broker(run_id), title="t", body="b",
                                  labels=["P3"])["ok"])

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
        self.assertTrue(self.send(server, title="ok", body="b", labels=["bug", "P3"])["ok"])

    def test_a_label_outside_the_triage_list_is_refused_with_nothing_filed(self):
        # The allowlist is what stops a finding being labelled into the fixer's queue.
        _, run_id = self.run_row()
        server = self.broker(run_id)
        for labels in (["P0"], ["wontfix"], ["bug", "fix-me"], ["ready-for-fix"]):
            with self.subTest(labels=labels):
                answer = self.send(server, title="t", body="b", labels=labels)
                self.assertFalse(answer["ok"], answer)
                self.assertIn("triage list", json.dumps(answer))
        self.assertEqual(self.posts, [])
        with Supervisor(self.db)._connect() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM filed_issues").fetchone()[0], 0)

    def test_one_title_per_pr_and_three_per_review(self):
        _, run_id = self.run_row()
        server = self.broker(run_id)
        self.assertTrue(self.send(server, title="Same thing", body="b", labels=["bug"])["ok"])
        again = self.send(server, title="  same   THING ", body="b", labels=["bug"])
        self.assertFalse(again["ok"])
        self.assertIn("already filed", again["error"])
        for n in (2, 3):
            self.assertTrue(self.send(server, title=f"t{n}", body="b", labels=["bug"])["ok"])
        over = self.send(server, title="t4", body="b", labels=["bug"])
        self.assertIn("at most 3", over["error"])
        self.assertEqual(len(self.posts), 3)

    def test_only_the_reviewer_may_file(self):
        _, run_id = self.run_row()
        for role in ("fixer", "issue_fixer", "triage"):
            with self.subTest(role=role):
                answer = self.send(self.broker(run_id, role=role), title="t", body="b",
                                   labels=["bug"])
                self.assertFalse(answer["ok"])
        with self.assertRaises(broker.BrokerDenied):
            broker.authorize(self.loop, repo=REPO, number=7, head=HEAD, role="fixer",
                             branch="fix-7", operation="file_issue")
        self.assertEqual(self.posts, [])

    def test_an_unknown_post_is_uncertain_and_never_refiled(self):
        _, run_id = self.run_row()
        server = self.broker(run_id)
        self.fail_post = True
        answer = self.send(server, title="t", body="b", labels=["bug"])
        self.assertIn("outcome is unknown", answer["error"])
        self.fail_post = False
        self.assertIn("already filed", self.send(server, title="t", body="b", labels=["bug"])["error"])
        self.assertEqual(len(self.posts), 1)

    def host_filing(self, sup, seat, title, state):
        """A host filing (#914, #1864) for a ``seat`` run on PR 7, its POST left in ``state``."""
        sup.enqueue(f"{REPO}:7:{HEAD}:{seat}", REPO, 7, HEAD, seat)
        with ledger.connect(self.db) as con:
            run = con.execute("SELECT id FROM runs WHERE seat=?", (seat,)).fetchone()[0]
        seq = sup.record_shadow_filing(run, REPO, 7, HEAD, title, 1)
        if state != "recorded":                           # "recorded": its POST still in flight
            sup.filed_issue_status(run, seq, state, error="POST outcome unknown: 502")
        return run, seq

    def test_a_host_filing_left_uncertain_does_not_block_the_reviewers_own(self):
        # #1360: shadow claims, GitHub 502s, then the reviewer files the same finding.
        sup, run_id = self.run_row()
        shadow, seq = self.host_filing(sup, "shadow", "Same Finding", "uncertain")
        server = self.broker(run_id)
        answer = self.send(server, title="same  finding", body="b", labels=["bug"])
        self.assertEqual(answer, {"ok": True, "result": {"accepted": True, "issue": 301}})
        self.assertEqual(len(self.posts), 1)
        with sup._connect() as con:
            kept = con.execute("SELECT state FROM filed_issues WHERE run_id=? AND seq=?",
                               (shadow, seq)).fetchone()[0]
            self.assertEqual(kept, "uncertain")               # still never replayed
            self.assertEqual(run_supervisor.write_evidence(con, shadow),
                             "filed issue outcome unknown")
        # The reviewer's own filing now holds the title: a retry is still refused.
        self.assertIn("already filed",
                      self.send(server, title="Same finding", body="b", labels=["bug"])["error"])
        self.assertEqual(len(self.posts), 1)

    def test_an_adjudicator_filing_left_uncertain_does_not_block_the_reviewers_own(self):
        # #1360, #1864: the host files an adjudicator's issue the same way as a shadow's.
        sup, run_id = self.run_row()
        adjudicator, seq = self.host_filing(sup, "adjudicator", "Same finding", "uncertain")
        answer = self.send(self.broker(run_id), title="Same finding", body="b", labels=["bug"])
        self.assertEqual(answer, {"ok": True, "result": {"accepted": True, "issue": 301}})
        self.assertEqual(len(self.posts), 1)
        with sup._connect() as con:
            kept = con.execute("SELECT state FROM filed_issues WHERE run_id=? AND seq=?",
                               (adjudicator, seq)).fetchone()[0]
            self.assertEqual(kept, "uncertain")               # still never replayed
            self.assertEqual(run_supervisor.write_evidence(con, adjudicator),
                             "filed issue outcome unknown")

    def assert_in_flight_blocks(self, seat):
        """A ``recorded`` host row is a POST that may still complete: it keeps the title."""
        sup, run_id = self.run_row()
        run, seq = self.host_filing(sup, seat, "Same finding", "recorded")
        answer = self.send(self.broker(run_id), title="Same finding", body="b", labels=["bug"])
        self.assertIn("already filed", answer.get("error", ""), answer)
        self.assertEqual(self.posts, [])
        with sup._connect() as con:
            key = con.execute("SELECT title_key FROM filed_issues WHERE run_id=? AND seq=?",
                              (run, seq)).fetchone()[0]
        self.assertEqual(key, "same finding")

    def test_a_shadow_filing_still_in_flight_blocks_the_reviewers_title(self):
        self.assert_in_flight_blocks("shadow")

    def test_an_adjudicator_filing_still_in_flight_blocks_the_reviewers_title(self):
        self.assert_in_flight_blocks("adjudicator")

    def test_a_host_filing_posted_still_blocks_the_reviewers_title(self):
        sup, run_id = self.run_row()
        self.host_filing(sup, "shadow", "Same finding", "posted")
        answer = self.send(self.broker(run_id), title="Same finding", body="b", labels=["bug"])
        self.assertIn("already filed", answer["error"])
        self.assertEqual(self.posts, [])

    def test_a_lost_post_response_is_write_evidence_so_the_run_is_not_retried(self):
        sup, run_id = self.run_row()
        server = self.broker(run_id)
        self.fail_post = True
        self.assertIn("outcome is unknown", self.send(server, title="t", body="b",
                                                      labels=["bug"])["error"])
        with sup._connect() as con:
            self.assertEqual(run_supervisor.write_evidence(con, run_id),
                             "filed issue outcome unknown")
            con.execute("UPDATE runs SET owner='w' WHERE id=?", (run_id,))
        with self.assertRaises(ValueError):
            sup.retry(run_id)
        sup.complete_uncertain(run_id, "w", 1, "review failed", retry=True)
        with sup._connect() as con:
            self.assertNotEqual(con.execute("SELECT state FROM runs WHERE id=?",
                                            (run_id,)).fetchone()[0], "waiting")

    def test_a_posted_issue_is_a_known_outcome_and_not_write_evidence(self):
        sup, run_id = self.run_row()
        server = self.broker(run_id)
        self.send(server, title="t", body="b", labels=["bug"])
        with sup._connect() as con:
            self.assertIsNone(run_supervisor.write_evidence(con, run_id))

    def test_a_finding_on_an_automatic_fix_is_one_deeper_and_says_a_person_decides(self):
        sup, run_id = self.run_row()
        # Issue #301 was filed (depth 1); the PR under review fixes it.
        self.send(self.broker(run_id), title="first", body="b", labels=["bug"])
        self.pr["head"]["ref"] = "diaktoros/issue-301"            # the PR fixing #301
        answer = self.send(self.broker(run_id, branch="diaktoros/issue-301"),
                           title="second", body="b", labels=["bug"])
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
            blocks.normalize_triage(loop["triage"], loop, "f")


class Prompt(unittest.TestCase):
    def test_the_reviewer_is_told_the_label_list_and_to_file_before_reviewing(self):
        facts = dict(repo=REPO, pr=7, url="u", head=HEAD, cap=3, round=1, reviewer_agent="a",
                     fixer_agent="b")
        text = prompts.render_isolated("reviewer", issue_labels="this list only: `P3`", **facts)
        self.assertIn("File each one yourself, before the review", text)
        self.assertIn("labels from this list only: `P3`", text)
        self.assertIn("the labels the list in parentheses asks for", text)
        self.assertIn("**Issues filed**", text)
        from diaktoros import trusted_turn
        tools = trusted_turn.tool_instructions("reviewer")
        self.assertIn("broker_client file_issue --title", tools)
        self.assertIn("at most 3 per review", tools)
        self.assertNotIn("file_issue", trusted_turn.tool_instructions("fixer"))

    def test_the_tool_text_asks_for_a_type_label_only_when_the_list_has_one(self):
        """#1462: the broker requires no type label from a priorities-only list, so the tool
        text must not ask for one unconditionally."""
        from diaktoros import trusted_turn
        tools = " ".join(trusted_turn.tool_instructions("reviewer").split())
        self.assertNotIn("otherwise one type label and one priority label", tools)
        self.assertIn("what the list line in your instructions says to send", tools)

    def test_the_prompt_the_reviewer_receives_states_the_cap_and_does_not_demand_two(self):
        """The reviewer's real prompt (``isolated_prompt``) names the loop's label cap, and no
        longer says "exactly one priority label", which a cap of 1 can never satisfy."""
        import test_ci_coverage as tcc
        import test_required_checks as trc
        for most in (1, 3):
            with self.subTest(max_labels=most):
                loop = config.normalize({**trc.raw(), "id": "w"})
                loop["triage"] = {"labels": ["bug", "P3"], "max_labels": most}
                with mock.patch.object(gh, "api", return_value=None):
                    text = " ".join(tcc.reviewer_prompt(loop, None).split())
                self.assertIn(f"`bug`, `P3` (at most {most} per issue; ", text)
                self.assertIn("send the type label alone" if most == 1 else "send a type label and a priority label", text)
                self.assertNotIn("exactly one priority label", text)


class ClientBounds(unittest.TestCase):
    """The sandbox client refuses a bad title or body before it sends anything, so the turn's one
    write is not spent on a request the broker would refuse (the broker checks again)."""

    def client(self, title, body):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "body"
            path.write_text(body)
            argv = ["broker_client", "file_issue", "--title", title, "--body-file", str(path),
                    "--label", "bug"]
            with mock.patch.object(broker_client, "call", return_value={"ok": True}) as call, \
                    mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                try:
                    broker_client.main()
                    code = 0
                except SystemExit as exc:
                    code = exc.code
        return code, call, err.getvalue()

    def test_a_bad_title_or_body_is_refused_before_sending(self):
        for name, title, body in (
                ("blank title", "  ", "b"),
                ("two lines", "a\nb", "b"),
                ("long title", "x" * (broker_client.ISSUE_TITLE_MAX + 1), "b"),
                ("empty body", "t", " \n"),
                ("huge body", "t", "x" * (broker_client.FILED_ISSUE_BODY_MAX + 1))):
            with self.subTest(name):
                code, call, err = self.client(title, body)
                self.assertEqual(code, 2, err)
                self.assertIn("file_issue refused before sending", err)
                call.assert_not_called()

    def test_a_good_request_is_sent_with_its_title_trimmed(self):
        code, call, _err = self.client(" A title ", "the body")
        self.assertEqual(code, 0)
        call.assert_called_once_with("file_issue", verdict="A title", body="the body",
                                     labels=["bug"])


if __name__ == "__main__":
    unittest.main()
