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

from diaktoros import ledger  # noqa: E402
from diaktoros import state as state_mod  # noqa: E402
from diaktoros import (broker, broker_ipc, config, fix_hold, gate, gh, run_supervisor, seat_model,  # noqa: E402
                         safe_push, trusted_turn)
from diaktoros.run_supervisor import Supervisor  # noqa: E402

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
                    "seats": {"reviewer": {"route": "widgets-review", "profile": "critic",
                                           "login": "review"},
                              "fixer": {"route": "widgets-fix", "profile": "coder",
                                        "login": "fix"}},
                    "triage": {"route": "widgets-triage", "profile": "arbiter",
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
        self.assertEqual(config.seat_profile(self.loop, "issue_fixer"), "coder")
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
        self.open_prs = []
        self.posted = []
        self.origin = None          # the PR a seat filed the issue from (#324)
        self.origin_pr = {"number": 7, "state": "open", "merged": False}

        def api(loop, path, method="GET", body=None, login=None):
            if "/pulls/" in path:
                return self.origin_pr
            if path.endswith(f"/git/ref/heads/{self.loop['base']}"):
                return {"ref": "refs/heads/main", "object": {"sha": BASE}}
            if method == "POST":
                self.posted.append((path, body, login))
                return {"id": 1}
            return self.live
        for target, name, value in (
                (fix_hold, "origin_pr", lambda loop, number: self.origin),
                (gh, "open_prs_read", lambda loop: (self.open_prs, "")),
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

    def test_an_enqueue_failure_is_held_with_the_base_it_failed_at(self):
        def boom(*a, **k):
            raise OSError("boom")
        notices = []
        with mock.patch.object(gate, "enqueue_isolated", boom), \
                mock.patch.object(self.module, "_fix_notice",
                                  lambda loop, n, base, outcome: notices.append((n, base, outcome))):
            log = self.run_gate(self.labeled(label="Agent-Fix"))
        self.assertIn("fix held: isolated worker unavailable: OSError: boom", log)
        self.assertNotIn("UnboundLocalError", log)
        self.assertEqual(notices, [(12, BASE, "held — isolated worker unavailable: OSError: boom")])

    def test_nobody_else_and_nothing_else_starts_a_fix(self):
        log = self.run_gate(self.labeled(sender="owner"))       # the author is not a maintainer
        self.assertIn("not in triage.maintainers", log)
        self.run_gate(self.labeled(label="bug"))
        self.live = issue(author="drive-by")
        self.run_gate(self.labeled())
        self.live = issue(labels=())                            # removed again since
        self.run_gate(self.labeled())
        self.assertEqual(self.enqueued, [])

    def test_an_open_pr_that_already_fixes_the_issue_skips_it_with_one_comment(self):
        self.open_prs = [{"number": 3, "body": "Fixes #120"}, {"number": 4, "body": "see #12"},
                         {"number": 5, "body": "Intro\n\nCloses #12."}]
        log = self.run_gate(self.labeled())
        self.assertIn("PR #5 already fixes it", log)
        self.assertEqual(self.enqueued, [])
        self.assertEqual(len(self.posted), 1)
        path, body, login = self.posted[0]
        self.assertTrue(path.endswith("/issues/12/comments"))
        self.assertEqual(body, {"body": "PR #5 already fixes this; not handing it to the fixer."})
        self.assertEqual(login, "review")

    def test_unrelated_open_prs_do_not_block_and_an_unreadable_listing_holds(self):
        self.open_prs = [{"number": 3, "body": "Fixes #120"}, {"number": 4, "body": None}]
        self.run_gate(self.labeled())
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual(self.posted, [])
        self.enqueued.clear()
        with mock.patch.object(gh, "open_prs_read", lambda loop: (None, "boom")):
            log = self.run_gate(self.labeled())
        self.assertIn("unreadable", log)
        self.assertEqual(self.enqueued, [])

    def test_a_failed_wait_comment_is_retried_on_a_later_sweep(self):
        self.origin = 7
        real = gh.api
        with mock.patch.object(
                gh, "api", lambda loop, path, method="GET", body=None, login=None:
                None if method == "POST" else real(loop, path, method, body, login)):
            self.run_gate(self.labeled())
        st = state_mod.state_for(self.loop)
        self.assertEqual((st.fix_holds(), st.fix_hold_said(12), self.posted), ({12: 7}, False, []))
        fix_hold.sweep(self.loop, st)                   # GitHub is back: the comment is retried
        self.assertEqual(len(self.posted), 1)
        self.assertTrue(st.fix_hold_said(12))
        fix_hold.sweep(self.loop, st)
        self.assertEqual(len(self.posted), 1)

    def test_a_finding_from_an_open_pr_is_held_until_it_merges(self):
        self.origin = 7
        self.run_gate(self.labeled())
        self.assertEqual(self.enqueued, [])
        st = state_mod.state_for(self.loop)
        self.assertEqual(st.fix_holds(), {12: 7})
        self.assertEqual([b["body"] for _p, b, _l in self.posted],
                         ["waiting for #7 to merge — this finding is about code only on that branch"])
        fix_hold.sweep(self.loop, st)                   # still open: no second comment, no turn
        self.run_gate(self.labeled())
        self.assertEqual((len(self.posted), self.enqueued), (1, []))
        # the PR merges: the watchdog sweep queues the fix from the base and clears the hold
        self.origin_pr = {"number": 7, "state": "closed", "merged": True}
        self.assertEqual(fix_hold.sweep(self.loop, st), [])
        self.assertEqual(self.enqueued, [("issue_fixer", 12, BASE, "issue-fix")])
        self.assertEqual(st.fix_holds(), {})

    def test_a_still_open_origin_stays_held_in_the_sweep(self):
        self.origin = 7
        self.run_gate(self.labeled())
        st = state_mod.state_for(self.loop)
        fix_hold.sweep(self.loop, st)
        self.assertEqual((self.enqueued, st.fix_holds()), ([], {12: 7}))

    def test_an_origin_closed_unmerged_drops_the_hand_off_with_a_comment(self):
        self.origin = 7
        self.run_gate(self.labeled())
        st = state_mod.state_for(self.loop)
        self.origin_pr = {"number": 7, "state": "closed", "merged": False}
        fix_hold.sweep(self.loop, st)
        self.assertEqual((self.enqueued, st.fix_holds()), ([], {}))
        self.assertEqual(len(self.posted), 2)           # the wait comment, then the moot one
        self.assertIn("closed without merging", self.posted[1][1]["body"])
        # labelled again with the origin already closed: comment, no turn
        self.run_gate(self.labeled())
        self.assertEqual((self.enqueued, len(self.posted)), ([], 3))

    def test_an_unreadable_origin_is_retried_by_the_sweep_never_guessed(self):
        self.origin = 7
        self.origin_pr = None
        log = self.run_gate(self.labeled())
        self.assertIn("unreadable", log)
        st = state_mod.state_for(self.loop)
        self.assertEqual((self.enqueued, self.posted, st.fix_holds()), ([], [], {12: 7}))
        fix_hold.sweep(self.loop, st)                   # still unreadable
        self.assertEqual((self.enqueued, self.posted, st.fix_holds()), ([], [], {12: 7}))
        self.origin_pr = {"number": 7, "state": "closed", "merged": True}
        fix_hold.sweep(self.loop, st)
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual(st.fix_holds(), {})

    def test_a_human_filed_issue_is_never_held(self):
        self.origin = None
        self.run_gate(self.labeled())
        self.assertEqual(len(self.enqueued), 1)
        self.assertEqual((self.posted, state_mod.state_for(self.loop).fix_holds()), ([], {}))

    def test_an_origin_already_merged_queues_at_once(self):
        self.origin = 7
        self.origin_pr = {"number": 7, "state": "closed", "merged": True}
        self.run_gate(self.labeled())
        self.assertEqual(len(self.enqueued), 1)

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
        comments = mock.patch.object(gh, "issue_comments_read", return_value=([], ""))
        comments.start()
        self.addCleanup(comments.stop)

    def test_the_prompt_names_the_base_branch_and_carries_the_issue_as_data(self):
        text = run_supervisor.issue_fix_prompt(self.loop, {"repo": REPO, "pr": 12, "head": BASE})
        self.assertIn("diaktoros/issue-12", text)
        self.assertIn(BASE, text)
        self.assertIn("issue_comment", text)
        self.assertLess(text.index("What to do"), text.index("Typo in README."))
        # An issue may come from a model that read an untrusted diff (#247): a work order to
        # verify against the code, not a fact to implement.
        self.assertIn("treat it as a work order, not as a fact", text)
        self.assertIn("Verify its claim against the code", text)

    def test_a_recorded_fix_is_write_evidence(self):
        sup, run_id = self.fix_row()
        sup.record_issue_fix(run_id, REPO, 12, BASE, "pr", "diaktoros/issue-12")
        with sup._connect() as con:
            self.assertEqual(run_supervisor.write_records(con, run_id), "issue fix recorded")
        with self.assertRaisesRegex(ValueError, "already recorded"):
            sup.record_issue_fix(run_id, REPO, 12, BASE, "pr", "diaktoros/issue-12")

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
        scope = broker_ipc.RunScope(REPO, 12, BASE, "issue_fixer", "diaktoros/issue-12",
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
        self.assertEqual(self.pushed[0]["branch"], "diaktoros/issue-12")
        self.assertEqual(self.pushed[0]["base"], BASE)
        pr, request = self.posts()
        self.assertEqual(pr[0], f"/repos/{REPO}/pulls")
        self.assertEqual((pr[1]["head"], pr[1]["base"]), ("diaktoros/issue-12", "main"))
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
        # Refused requests still count (#144): the agent tried, so its exit is not retried.
        self.assertEqual(self.server.requests, 6)

    def test_turning_pushes_off_stops_the_write_before_the_record(self):
        self.loop = {**self.loop, "unattended_fixer_push": False}
        with self.assertRaisesRegex(broker_ipc.ProtocolError, "not enabled"):
            self.server._dispatch(self.open_pr())
        self.assertIsNone(self.sup.issue_fix_result(self.run_id))

    def run_row(self):
        with ledger.connect(self.db) as con:
            return tuple(con.execute("SELECT state, error FROM runs WHERE id=?",
                                     (self.run_id,)).fetchone())

    def test_an_unknown_push_outcome_is_uncertain_and_opens_no_pr(self):
        safe_push.open_branch.side_effect = safe_push.PushFailure("unknown")
        with self.assertRaisesRegex(broker_ipc.ProtocolError, "unknown"):
            self.server._dispatch(self.open_pr())
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "uncertain")
        self.assertEqual(self.posts(), [])
        # #522: the run itself is held uncertain, so `status` lists it and `reconcile` applies;
        # it never ends `succeeded` with its write unknown.
        self.assertEqual(self.run_row(), ("uncertain", "post-write push quarantine: unknown"))

    def test_an_unknown_pr_create_holds_the_run_uncertain(self):
        def api(loop, path, method="GET", body=None, login=None):
            if path == f"/repos/{REPO}/pulls" and method == "POST":
                raise OSError("lost response")
            return {"login": login, "id": 3} if path == "/user" else self.live
        gh.api.side_effect = api
        with self.assertRaisesRegex(broker_ipc.ProtocolError, "PR could not be confirmed"):
            self.server._dispatch(self.open_pr())
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "uncertain")
        self.assertEqual(self.run_row(),
                         ("uncertain", "post-write push quarantine: pr_create_unknown"))

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


class WorkerLaunch(Base):
    """The real ``_run_production`` for an issue-fix row, with only the outside world faked: the
    seat's model resolves through the real ``resolve_seat`` (the profile lookup is the fake), so a
    seat name the resolver does not know fails here exactly as it would in production."""

    def test_an_issue_fix_turn_resolves_the_fixers_model_and_launches(self):
        sup, run_id = self.fix_row(state="launching")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET owner='w' WHERE id=?", (run_id,))
        resolved, launched = [], []
        inference = mock.Mock(upstream="https://api.example/v1/chat/completions", key="k",
                              model="m", api_mode="chat_completions", proxy_model="",
                              client_identity="", provider="custom", profile="coder")
        inference.credential_provider.return_value = None

        def resolve_profile(profile, seat, settings):
            resolved.append((profile, seat))
            return inference

        def run_turn(loop, scope, **kw):
            launched.append((scope.role, scope.branch, scope.head))
            return 0
        sup.production_config = self.root / "runtime.json"
        with mock.patch.object(seat_model, "load_runtime", return_value={
                    "source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}), \
                mock.patch.object(seat_model, "resolve_profile",
                                  side_effect=resolve_profile), \
                mock.patch.object(config, "by_repo", return_value=self.loop), \
                mock.patch.object(gh, "api", return_value=issue()), \
                mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
                mock.patch.object(trusted_turn, "run_turn", side_effect=run_turn), \
                mock.patch.object(sup, "recover"), \
                mock.patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            sup._run_production(run_id, "w")
        with sup._connect() as con:
            row = con.execute("SELECT state,error FROM runs WHERE id=?", (run_id,)).fetchone()
        self.assertNotIn("seat model unresolved", row["error"] or "", dict(row))
        self.assertEqual(resolved, [("coder", "fixer")])       # the fixer seat's own profile
        self.assertEqual(launched, [("issue_fixer", "diaktoros/issue-12", BASE)])


class OpenBranchGuards(Base):
    """#233: open_branch's own guards, each pinned — the branch name and the fixer's identity."""

    def setUp(self):
        super().setUp()
        import base64
        import hashlib
        data = b"fixed\n"
        self.manifest = {"base_head": BASE, "message": "Fix the typo",
                         "files": [{"path": "README.md",
                                    "content_b64": base64.b64encode(data).decode(),
                                    "sha256": hashlib.sha256(data).hexdigest()}]}
        self.user = {"login": "fix", "id": 3}
        for target, name, value in (
                (broker, "authorize_issue_fix", mock.Mock(return_value="fix")),
                (safe_push, "_api", mock.Mock(side_effect=lambda loop, path, **kw: self.user)),
                (safe_push, "_git_cas", mock.Mock(side_effect=AssertionError("no push")))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def open(self, branch="diaktoros/issue-12"):
        return safe_push.open_branch(self.loop, repo=REPO, number=12, base=BASE, branch=branch,
                                     manifest=self.manifest)

    def test_only_the_issues_own_branch_may_be_pushed(self):
        for branch in ("main", "diaktoros/issue-13", "diaktoros/issue-12/x", "fix-7"):
            with self.subTest(branch=branch), self.assertRaisesRegex(broker.BrokerDenied,
                                                                     "unsafe branch ref"):
                self.open(branch)
        safe_push._git_cas.assert_not_called()

    def test_the_token_must_resolve_to_the_fixer_before_anything_is_built(self):
        for user, reason in (({"login": "someone-else", "id": 3}, "fixer identity changed"),
                             ({"login": "fix", "id": 0}, "fixer identity changed"),
                             ({"login": "fix"}, "fixer identity changed")):
            with self.subTest(user=user):
                self.user = user
                with self.assertRaisesRegex(broker.BrokerDenied, reason):
                    self.open()
        safe_push._git_cas.assert_not_called()


class OpenBranchExisting(OpenBranchGuards):
    """An existing diaktoros/issue-N branch is a pre-write denial, not an uncertain push."""

    def read(self, result):
        patch = mock.patch.object(safe_push.gh, "fetch", mock.Mock(return_value=result))
        patch.start()
        self.addCleanup(patch.stop)

    def test_an_existing_branch_is_denied_before_any_write(self):
        self.read(({"ref": "refs/heads/diaktoros/issue-12", "object": {"sha": "a" * 40}}, ""))
        with self.assertRaisesRegex(broker.BrokerDenied,
                                    "diaktoros/issue-12 already exists — inspect it, and see docs/issues.md"):
            self.open()
        safe_push._git_cas.assert_not_called()

    def test_a_failed_read_is_denied_before_any_write(self):
        for result in ((None, "HTTP 500"), (None, "URLError: down"), (None, ""), ([], "")):
            with self.subTest(result=result):
                self.read(result)
                with self.assertRaises(broker.BrokerDenied):
                    self.open()
        safe_push._git_cas.assert_not_called()

    def test_a_404_proceeds_to_the_push(self):
        self.read((None, "HTTP 404 Not Found"))
        with self.assertRaises(broker.BrokerDenied):    # the stubbed push fails; it was reached
            self.open()
        safe_push._git_cas.assert_called_once()


class ConfirmAfterPush(OpenBranchGuards):
    """#522: GitHub may not serve a just-pushed ref at once. The read-back waits it out, so a
    landed issue-fix branch is ``published`` (and its PR opens), not ``unknown``."""

    NEW = "e" * 40

    def setUp(self):
        super().setUp()
        self.reads = []
        self.sleeps = []
        self.visible_after = 2                    # reads that 404 before the ref shows

        def api(loop, path, **kw):
            if path == "/user":
                return self.user
            self.reads.append(path)
            if len(self.reads) <= self.visible_after:
                raise broker.BrokerDenied("GitHub read request failed")      # 404, as _api says
            return {"ref": "refs/heads/diaktoros/issue-12", "object": {"sha": self.NEW}}

        def cas(loop, repo, branch, base, files, message, login, identity, *, before_push,
                **kw):
            before_push(self.NEW)
        for target, name, value in (
                (safe_push, "_api", mock.Mock(side_effect=api)),
                (safe_push, "_git_cas", mock.Mock(side_effect=cas)),
                (safe_push, "_audit", mock.Mock()),
                (safe_push.time, "sleep", mock.Mock(side_effect=self.sleeps.append)),
                (safe_push.gh, "fetch", mock.Mock(return_value=(None, "HTTP 404 Not Found")))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_ref_that_shows_late_is_published(self):
        result = self.open()
        self.assertEqual((result["outcome"], result["new_head"]), ("published", self.NEW))
        self.assertEqual(len(self.reads), 3)
        self.assertEqual(self.sleeps, list(safe_push.CONFIRM_WAITS[:2]))

    def test_a_ref_that_never_shows_is_unknown_after_the_window(self):
        self.visible_after = 99
        with self.assertRaises(safe_push.PushFailure) as failed:
            self.open()
        self.assertEqual(failed.exception.outcome, "unknown")
        self.assertEqual(self.sleeps, list(safe_push.CONFIRM_WAITS))


class ConfirmRef(unittest.TestCase):
    """The shared read-back (#522): what ends the wait, and what it never waits for."""

    def confirm(self, answers, prior="a" * 40, push_failed=False):
        reads, sleeps = list(answers), []
        with mock.patch.object(safe_push, "_read_ref", side_effect=lambda *a: reads.pop(0)), \
                mock.patch.object(safe_push.time, "sleep", side_effect=sleeps.append):
            observed = safe_push._confirm_ref({}, "/ref", "b", "fix", "n" * 40, prior,
                                              push_failed=push_failed)
        return observed, sleeps

    def test_the_old_head_is_lag_after_a_reported_push(self):
        self.assertEqual(self.confirm(["a" * 40, "n" * 40]), ("n" * 40, [1]))

    def test_another_commit_ends_the_wait_at_once(self):
        self.assertEqual(self.confirm(["c" * 40]), ("c" * 40, []))

    def test_a_refused_push_that_left_the_old_head_is_read_once(self):
        self.assertEqual(self.confirm(["a" * 40], push_failed=True), ("a" * 40, []))

    def test_a_missing_ref_is_waited_out_even_after_a_failed_push(self):
        # A lost response: Git raised, the ref may still have landed.
        self.assertEqual(self.confirm([None, "n" * 40], push_failed=True), ("n" * 40, [1]))


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

            def cas(head, branch="diaktoros/issue-12", content=b"fixed"):
                return safe_push._git_cas(loop, REPO, branch, head, [("README.md", content)],
                                          "Fix the typo", "fix", identity, remote=remote,
                                          from_branch="main")
            with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1",
                                              "GIT_CONFIG_KEY_0": "protocol.file.allow",
                                              "GIT_CONFIG_VALUE_0": "never",
                                              "GIT_CONFIG_GLOBAL": "/does/not/exist"}):
                new = cas(pinned)              # main moved on since: the pinned commit still counts
                self.assertEqual(git("--git-dir", remote, "rev-parse",
                                     "refs/heads/diaktoros/issue-12"), new)
                self.assertEqual(git("--git-dir", remote, "rev-list", "--parents", "-n", "1", new),
                                 f"{new} {pinned}")
                self.assertEqual(git("--git-dir", remote, "rev-parse", "refs/heads/main"), later)
                with self.assertRaises(broker.BrokerDenied):
                    cas(pinned, content=b"another fix")   # the branch exists: never overwritten
                self.assertEqual(git("--git-dir", remote, "rev-parse",
                                     "refs/heads/diaktoros/issue-12"), new)
                with self.assertRaisesRegex(broker.BrokerDenied, "not on the base branch"):
                    cas(stray, branch="diaktoros/issue-13")


if __name__ == "__main__":
    unittest.main()
