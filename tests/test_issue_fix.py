"""Issue-triggered fixes (#214): a maintainer's fix label hands an issue to the fixer seat.

No real GitHub, model, Hermes or ~/.hermes: GitHub is mocked and the ledger lives in a private
temporary directory. The one Git test runs real ``git`` against local bare repositories through
``safe_push``'s private remote seam. What is pinned: only a maintainer's label on an allowlisted
author's issue starts a turn, only with unattended fixer pushes on; the turn's one write is a
fresh branch (never an existing one) with its PR and review request, or one comment; everything
is recorded before it is written.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from review_loop import ledger  # noqa: E402
from review_loop import (broker, broker_ipc, config, gate, gh, run_supervisor,  # noqa: E402
                         safe_push, trusted_turn)
from review_loop.run_supervisor import Supervisor  # noqa: E402

REPO = "acme/widgets"
BASE = "c" * 40
LABEL = "agent-fix"


def issue(number=12, author="owner", state="open", labels=(LABEL,), body="Typo in README."):
    return {"number": number, "state": state, "user": {"login": author}, "title": "Fix the typo",
            "body": body, "labels": [{"name": name} for name in labels],
            "html_url": f"https://github.com/{REPO}/issues/{number}"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        tokens = {}
        for login in ("read", "review", "fix"):
            path = self.root / f"{login}.pat"
            path.write_text("DUMMY_SECRET_" + login)
            path.chmod(0o600)
            tokens[login] = str(path)
        self.raw = {"id": "widgets", "repo": REPO, "fixers": ["fix"], "reviewers": ["review"],
                    "tokens": tokens, "read_token": "read", "host": "https://gw.example",
                    "state_dir": str(self.root / "state"), "unattended_fixer_push": True,
                    "seats": {"reviewer": {"route": "widgets-review", "profile": "vex",
                                           "login": "review"},
                              "fixer": {"route": "widgets-fix", "profile": "drey",
                                        "login": "fix"}},
                    "triage": {"route": "widgets-triage", "profile": "tuck",
                               "authors": ["owner"], "labels": ["bug", "docs"],
                               "fix_label": LABEL, "maintainers": ["Jeremy"]}}
        self.loop = config.normalize(self.raw)
        self.db = self.root / "runs.sqlite"

    def fix_row(self, state="running", number=12):
        sup = Supervisor(self.db)
        sup.enqueue(f"{REPO}:{number}:{BASE}:issue_fixer:issue-fix", REPO, number, BASE,
                    "issue_fixer", turn_key="issue-fix")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state=? WHERE seat='issue_fixer'", (state,))
            if state in ("launching", "running"):
                con.execute("UPDATE runs SET launch_intent=? WHERE seat='issue_fixer'",
                            (time.time(),))
            return sup, con.execute("SELECT id FROM runs WHERE seat='issue_fixer'").fetchone()[0]


class Config(Base):
    def test_issue_fixes_need_a_label_maintainers_and_unattended_pushes(self):
        self.assertTrue(config.issue_fixes_enabled(self.loop))
        self.assertEqual(self.loop["triage"]["maintainers"], ["jeremy"])
        off = config.normalize({**self.raw, "unattended_fixer_push": False})
        self.assertFalse(config.issue_fixes_enabled(off))
        for name, change in (("no maintainers", {"maintainers": []}),
                             ("label triage could apply", {"fix_label": "docs"}),
                             ("maintainers without a label", {"fix_label": "",
                                                              "maintainers": ["x"]})):
            with self.subTest(name), self.assertRaises(config.ConfigError):
                config.normalize({**self.raw, "triage": {**self.raw["triage"], **change}})

    def test_the_issue_fixer_is_the_fixer_seat(self):
        self.assertEqual(config.seat_profile(self.loop, "issue_fixer"), "drey")
        self.assertEqual(config.seat_login(self.loop, "issue_fixer"), "fix")
        self.assertEqual(config.seat_concurrency(self.loop, "issue_fixer"), 1)


class Gate(Base):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("gate_triage_fix_under_test",
                                                      ROOT / "scripts" / "gate_triage.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.live = issue()
        self.enqueued = []

        def api(loop, path, method="GET", body=None, login=None):
            if path.endswith(f"/git/ref/heads/{self.loop['base']}"):
                return {"ref": "refs/heads/main", "object": {"sha": BASE}}
            return self.live
        for target, name, value in (
                (gate, "context", lambda payload: (self.loop, None)),
                (gh, "api", api),
                (gate, "enqueue_isolated",
                 lambda loop, seat, n, head, turn_key="": self.enqueued.append(
                     (seat, n, head, turn_key)) or "enqueued")):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def run_gate(self, payload) -> str:
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            self.module.main()
        return err.getvalue()

    def labeled(self, label=LABEL, sender="jeremy"):
        return {"action": "labeled", "label": {"name": label}, "issue": issue(),
                "sender": {"login": sender}, "repository": {"full_name": REPO}}

    def test_a_maintainers_fix_label_queues_one_fix_from_the_base_head(self):
        self.run_gate(self.labeled(label="Agent-Fix"))
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix")])

    def test_nobody_else_and_nothing_else_starts_a_fix(self):
        log = self.run_gate(self.labeled(sender="owner"))       # the author is not a maintainer
        self.assertIn("not in triage.maintainers", log)
        self.run_gate(self.labeled(label="bug"))
        self.live = issue(author="drive-by")
        self.run_gate(self.labeled())
        self.live = issue(labels=())                            # removed again since
        self.run_gate(self.labeled())
        self.assertEqual(self.enqueued, [])

    def test_without_unattended_pushes_the_label_is_named_not_acted_on(self):
        self.loop = {**self.loop, "unattended_fixer_push": False}
        log = self.run_gate(self.labeled())
        self.assertIn("fixer-push", log)
        self.assertEqual(self.enqueued, [])


class Worker(Base):
    def setUp(self):
        super().setUp()
        self.live = issue()
        patch = mock.patch.object(gh, "api", side_effect=lambda *a, **k: self.live)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_prompt_names_the_base_branch_and_carries_the_issue_as_data(self):
        text = run_supervisor.issue_fix_prompt(self.loop, {"repo": REPO, "pr": 12, "head": BASE})
        self.assertIn("review-loop/issue-12", text)
        self.assertIn(BASE, text)
        self.assertIn("issue_comment", text)
        self.assertLess(text.index("What to do"), text.index("Typo in README."))

    def test_a_recorded_fix_is_write_evidence(self):
        sup, run_id = self.fix_row()
        sup.record_issue_fix(run_id, REPO, 12, BASE, "pr", "review-loop/issue-12")
        with sup._connect() as con:
            self.assertEqual(run_supervisor.write_records(con, run_id), "issue fix recorded")
        with self.assertRaisesRegex(ValueError, "already recorded"):
            sup.record_issue_fix(run_id, REPO, 12, BASE, "pr", "review-loop/issue-12")

    def test_tools_advertise_only_the_issue_fixers_two_writes(self):
        tools = trusted_turn.tool_instructions("issue_fixer")
        self.assertIn("broker_client open_pr", tools)
        self.assertIn("broker_client issue_comment", tools)
        for other in ("broker_client review", "broker_client push", "ruling", "triage"):
            self.assertNotIn(other, tools)


class BrokerWrite(Base):
    def setUp(self):
        super().setUp()
        self.sup, self.run_id = self.fix_row()
        self.live = issue()
        self.calls = []

        def api(loop, path, method="GET", body=None, login=None):
            self.calls.append((path, method, body, login))
            if path == "/user":
                return {"login": login, "id": 3}
            if path == f"/repos/{REPO}/pulls" and method == "POST":
                return {"number": 40, "head": {"ref": body["head"]}}
            if method == "POST":
                return {"number": 40, "id": 9}
            return self.live
        self.pushed = []

        def open_branch(loop, **kw):
            self.pushed.append(kw)
            return {"new_head": "d" * 40, "login": "fix"}
        for target, name, value in ((gh, "api", mock.Mock(side_effect=api)),
                                    (config, "by_repo", mock.Mock(side_effect=lambda r: self.loop)),
                                    (safe_push, "open_branch", mock.Mock(side_effect=open_branch))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        scope = broker_ipc.RunScope(REPO, 12, BASE, "issue_fixer", "review-loop/issue-12",
                                    self.run_id, str(self.db))
        self.server = broker_ipc.RunBroker(self.loop, scope, self.root)

    def manifest(self):
        import base64
        import hashlib
        data = b"fixed\n"
        return {"base_head": BASE, "message": "Fix the typo",
                "files": [{"path": "README.md", "content_b64": base64.b64encode(data).decode(),
                           "sha256": hashlib.sha256(data).hexdigest()}]}

    def open_pr(self, **change):
        return json.dumps({"operation": "open_pr", "manifest": self.manifest(),
                           "title": "Fix the README typo", "body": "Fixed; checked the render.",
                           **change}).encode()

    def posts(self):
        return [(path, body) for path, method, body, _ in self.calls if method == "POST"]

    def test_open_pr_pushes_a_fresh_branch_opens_the_pr_and_requests_the_reviewer(self):
        self.server._dispatch(self.open_pr())
        self.assertEqual(self.pushed[0]["branch"], "review-loop/issue-12")
        self.assertEqual(self.pushed[0]["base"], BASE)
        pr, request = self.posts()
        self.assertEqual(pr[0], f"/repos/{REPO}/pulls")
        self.assertEqual((pr[1]["head"], pr[1]["base"]), ("review-loop/issue-12", "main"))
        self.assertIn("Fixes #12", pr[1]["body"])
        self.assertEqual(request, (f"/repos/{REPO}/pulls/40/requested_reviewers",
                                   {"reviewers": ["review"]}))
        result = self.sup.issue_fix_result(self.run_id)
        self.assertEqual((result["state"], result["pr_number"]), ("requested", 40))
        self.assertTrue(self.server.completed)
        with self.assertRaisesRegex(broker_ipc.ProtocolError, "already used"):
            self.server._dispatch(json.dumps({"operation": "issue_comment",
                                              "body": "x"}).encode())

    def test_bad_requests_are_refused_before_the_write_is_spent(self):
        for raw in (self.open_pr(title=""), self.open_pr(title="two\nlines"),
                    self.open_pr(body=""), self.open_pr(manifest={"base_head": BASE}),
                    json.dumps({"operation": "push", "manifest": self.manifest()}).encode(),
                    json.dumps({"operation": "review", "verdict": "APPROVE",
                                "body": "x"}).encode()):
            with self.subTest(raw=raw[:60]), self.assertRaises(
                    (broker_ipc.ProtocolError, broker.BrokerDenied)):
                self.server._dispatch(raw)
        self.assertEqual((self.pushed, self.posts()), ([], []))
        self.assertIsNone(self.sup.issue_fix_result(self.run_id))

    def test_turning_pushes_off_stops_the_write_before_the_record(self):
        self.loop = {**self.loop, "unattended_fixer_push": False}
        with self.assertRaisesRegex(broker_ipc.ProtocolError, "not enabled"):
            self.server._dispatch(self.open_pr())
        self.assertIsNone(self.sup.issue_fix_result(self.run_id))

    def test_an_unknown_push_outcome_is_uncertain_and_opens_no_pr(self):
        safe_push.open_branch.side_effect = safe_push.PushFailure("unknown")
        with self.assertRaisesRegex(broker_ipc.ProtocolError, "unknown"):
            self.server._dispatch(self.open_pr())
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "uncertain")
        self.assertEqual(self.posts(), [])

    def test_could_not_fix_is_one_signed_comment_on_the_issue(self):
        self.server._dispatch(json.dumps({"operation": "issue_comment",
                                          "body": "Needs a decision on the API."}).encode())
        (path, body), = self.posts()
        self.assertEqual(path, f"/repos/{REPO}/issues/12/comments")
        self.assertIn("Automated by", body["body"])
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "posted")
        self.assertEqual(self.pushed, [])

    def test_a_label_removed_since_denies_the_comment(self):
        self.live = issue(labels=())
        with self.assertRaises(broker.BrokerDenied):
            self.server._dispatch(json.dumps({"operation": "issue_comment",
                                              "body": "x"}).encode())
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "denied")
        self.assertEqual(self.posts(), [])


class RealBareBranch(unittest.TestCase):
    """``_git_cas`` with ``from_branch``: one commit on the pinned base, to a branch that must
    not exist yet — real git, local bare repositories."""

    def test_a_fresh_branch_from_the_pinned_base_and_never_over_an_existing_one(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            remote = str(Path(temp) / "remote.git")
            local = str(Path(temp) / "creator.git")

            def git(*args, **kwargs):
                return subprocess.check_output(["git", *args], text=True, **kwargs).strip()
            git("init", "-q", "--bare", "--initial-branch=main", remote)
            git("init", "-q", "--bare", "--initial-branch=main", local)
            blob = git("--git-dir", local, "hash-object", "-w", "--stdin", input="typo")
            tree = git("--git-dir", local, "mktree", input=f"100644 blob {blob}\tREADME.md\n")
            env = {**os.environ, "GIT_AUTHOR_NAME": "F", "GIT_AUTHOR_EMAIL": "f@example.org",
                   "GIT_COMMITTER_NAME": "F", "GIT_COMMITTER_EMAIL": "f@example.org"}
            pinned = git("--git-dir", local, "commit-tree", tree, input="base\n", env=env)
            later = git("--git-dir", local, "commit-tree", tree, "-p", pinned, input="later\n",
                        env=env)
            stray = git("--git-dir", local, "commit-tree", tree, input="stray\n", env=env)
            git("--git-dir", local, "push", "-q", remote, f"{later}:refs/heads/main")
            git("--git-dir", local, "push", "-q", remote, f"{stray}:refs/heads/elsewhere")
            loop = {"tokens": {"fix": str(Path(temp) / "dummy")}}
            Path(loop["tokens"]["fix"]).write_text("not-a-real-token")
            identity = {"name": "fix", "email": "3+fix@users.noreply.github.com"}

            def cas(head, branch="review-loop/issue-12", content=b"fixed"):
                return safe_push._git_cas(loop, REPO, branch, head, [("README.md", content)],
                                          "Fix the typo", "fix", identity, remote=remote,
                                          from_branch="main")
            with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1",
                                              "GIT_CONFIG_KEY_0": "protocol.file.allow",
                                              "GIT_CONFIG_VALUE_0": "never",
                                              "GIT_CONFIG_GLOBAL": "/does/not/exist"}):
                new = cas(pinned)              # main moved on since: the pinned commit still counts
                self.assertEqual(git("--git-dir", remote, "rev-parse",
                                     "refs/heads/review-loop/issue-12"), new)
                self.assertEqual(git("--git-dir", remote, "rev-list", "--parents", "-n", "1", new),
                                 f"{new} {pinned}")
                self.assertEqual(git("--git-dir", remote, "rev-parse", "refs/heads/main"), later)
                with self.assertRaises(broker.BrokerDenied):
                    cas(pinned, content=b"another fix")   # the branch exists: never overwritten
                self.assertEqual(git("--git-dir", remote, "rev-parse",
                                     "refs/heads/review-loop/issue-12"), new)
                with self.assertRaisesRegex(broker.BrokerDenied, "not on the base branch"):
                    cas(stray, branch="review-loop/issue-13")


if __name__ == "__main__":
    unittest.main()
