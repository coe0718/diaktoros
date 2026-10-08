"""A reviewer that could not see the whole change cannot approve it: the broker refuses (#93/#110).

The host records, per run, whether the reviewer's view of the change is complete — when it builds
the change record, in the run ledger and the run's host-built scope, never from the sandbox. The
broker then refuses an APPROVE for a run whose view is incomplete, before the one write is spent,
with a refusal the seat reads; a REQUEST_CHANGES still goes through. Offline: GitHub REST is
mocked, the socket is a real Unix domain socket and the ledger is the real SQLite run ledger.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import _ci_green  # noqa: E402  CI reads as green unless a test says otherwise
import dataclasses
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import ledger  # noqa: E402
from diaktoros import (broker_client, broker_ipc, config, gh, review_receipt,  # noqa: E402
                         run_supervisor, trusted_turn)
from diaktoros.run_supervisor import Supervisor  # noqa: E402
import test_pr_change as pc  # noqa: E402  (its World fakes gh.fetch for pr_change)

HEAD = "a" * 40
BASE = "b" * 40
REPO = "acme/widgets"
REASON = "the PR's file list could not be read (PR file page 1: HTTP 404 {})"
REFUSED = "the host could not show you the whole change"


class Broker(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        tokens = {}
        for login in ("read", "review", "fix"):
            path = self.root / (login + ".pat")
            path.write_text("DUMMY_" + login)
            tokens[login] = str(path)
        self.loop = {"repo": REPO, "base": "main", "state_dir": str(self.root),
                     "fixers": ["fix"], "reviewers": ["review"], "tokens": tokens,
                     "read_token": "read", "reviewer_seat": "review",
                     "seats": {"reviewer": {"login": "review"}, "fixer": {"login": "fix"}}}
        self.pr = {"number": 7, "state": "open", "draft": False, "user": {"login": "fix"},
                   "head": {"sha": HEAD, "ref": "fix-7", "repo": {"full_name": REPO}},
                   "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPO}}}
        self.posts = []
        patch = mock.patch.object(gh, "api", side_effect=_ci_green.green(self.api))
        patch.start()
        self.addCleanup(patch.stop)

    def api(self, loop, path, method="GET", body=None, login=None):
        if path == "/user":
            return {"login": login, "id": {"read": 1, "review": 2, "fix": 3}[login]}
        if method == "POST":
            self.posts.append(body)
            return {"id": 19}
        if path == f"/repos/{REPO}/pulls/7/reviews/19":
            state = {"APPROVE": "APPROVED",
                     "REQUEST_CHANGES": "CHANGES_REQUESTED"}[self.posts[-1]["event"]]
            return {"id": 19, "state": state, "commit_id": HEAD,
                    "user": {"id": 2, "login": "review"}}
        return self.pr

    def ledgered(self, partial):
        """A running reviewer run whose change record the host built, complete or not."""
        sup = Supervisor(self.root / "runs.sqlite")
        sup.enqueue("d", REPO, 7, HEAD, "reviewer")
        generation = review_receipt.generation_for(self.pr, self.loop, 7, HEAD)
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running', owner='w', generation=?", (generation,))
            run_id = con.execute("SELECT id FROM runs").fetchone()[0]
        sup.record_view(run_id, "w", partial)
        scope = broker_ipc.RunScope(REPO, 7, HEAD, "reviewer", "fix-7", run_id, str(sup.db),
                                    generation, partial_view=partial)
        return sup, scope

    def start(self, scope, **kwargs):
        server = broker_ipc.RunBroker(self.loop, scope, self.root, **kwargs)
        server.__enter__()
        thread = broker_ipc.serve_in_thread(server)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.close)
        return server

    def send(self, server, verdict, body="reviewed\nNot verified: nothing", **extra):
        """The sandbox's side: the real client over the real socket, when the request is legal."""
        if not extra:
            return broker_client.call("review", verdict=verdict, body=body,
                                      socket_path=str(server.socket_path))
        raw = json.dumps({"operation": "review", "verdict": verdict, "body": body,
                          **extra}).encode() + b"\n"
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(6)
            client.connect(str(server.socket_path))
            client.sendall(raw)
            client.shutdown(socket.SHUT_WR)
            return json.loads(client.recv(16384))

    def receipts(self, sup):
        with ledger.connect(sup.db) as con:
            return con.execute("SELECT state,review_id,verdict FROM review_receipts").fetchall()

    def test_incomplete_view_refuses_approve_then_request_changes_goes_through(self):
        sup, scope = self.ledgered(REASON)
        server = self.start(scope, require_receipt=True)
        refused = self.send(server, "APPROVE")
        self.assertFalse(refused["ok"])
        self.assertIn(REFUSED, refused["error"])
        self.assertIn("an approval is refused", refused["error"])
        self.assertIn("REQUEST_CHANGES", refused["error"])
        self.assertIn("HTTP 404", refused["error"])              # what was missing, host-worded
        self.assertEqual(self.posts, [])                          # no GitHub write at all
        self.assertEqual(self.receipts(sup), [])                  # no receipt claim either
        self.assertFalse(server.completed)
        # The capability is unspent: the seat's REQUEST_CHANGES in the same turn is the write.
        self.assertTrue(self.send(server, "REQUEST_CHANGES", "file list unavailable\nNot verified: nothing")["ok"])
        self.assertEqual([p["event"] for p in self.posts], ["REQUEST_CHANGES"])
        self.assertEqual(self.receipts(sup), [("confirmed", 19, "CHANGES_REQUESTED")])
        self.assertTrue(server.completed)

    def test_complete_view_approves(self):
        sup, scope = self.ledgered("")
        server = self.start(scope, require_receipt=True)
        self.assertEqual(self.send(server, "APPROVE"), {"ok": True, "result": {"accepted": True}})
        self.assertEqual([p["event"] for p in self.posts], ["APPROVE"])
        self.assertEqual(self.receipts(sup), [("confirmed", 19, "APPROVED")])

    def test_the_sandbox_cannot_set_or_clear_the_flag(self):
        sup, scope = self.ledgered(REASON)
        server = self.start(scope, require_receipt=True)
        for extra in ({"partial_view": ""}, {"view": "complete"}, {"partial_view": None},
                      {"complete": True}):
            with self.subTest(extra=extra):
                response = self.send(server, "APPROVE", **extra)
                self.assertFalse(response["ok"])
                self.assertEqual(response["error"], "unsupported request fields")
        self.assertIn(REFUSED, self.send(server, "APPROVE")["error"])
        self.assertEqual(self.posts, [])
        with ledger.connect(sup.db) as con:
            self.assertEqual(con.execute("SELECT partial_view FROM runs").fetchone(), (REASON,))
        # Not a socket field, and not a setting on a started broker: the scope is frozen.
        with self.assertRaises(dataclasses.FrozenInstanceError):
            server.scope.partial_view = ""
        # The host-side record is only the owning worker's to write, and only while it runs.
        with self.assertRaises(ValueError):
            sup.record_view(scope.run_id, "someone-else", "")
        with ledger.connect(sup.db) as con:
            self.assertEqual(con.execute("SELECT partial_view FROM runs").fetchone(), (REASON,))

    def test_the_ledger_refuses_even_when_the_scope_says_complete(self):
        # Defence in depth: the durable record is checked again inside the receipt claim, so a
        # host path that built its scope without the flag still cannot POST an approval.
        sup, scope = self.ledgered(REASON)
        stale = dataclasses.replace(scope, partial_view="")
        server = self.start(stale, require_receipt=True)
        self.assertIn(REFUSED, self.send(server, "APPROVE")["error"])
        ledger = review_receipt.ReceiptLedger(str(sup.db), scope.run_id, scope.generation)
        with self.assertRaisesRegex(review_receipt.ReceiptDenied, "approval refused"):
            review_receipt.submit(self.loop, stale, ledger, "APPROVE", "looks fine")
        self.assertEqual(self.posts, [])
        self.assertEqual(self.receipts(sup), [])

    def test_scope_flag_alone_refuses_an_unledgered_or_no_write_approval(self):
        scope = broker_ipc.RunScope(REPO, 7, HEAD, "reviewer", "fix-7", partial_view=REASON)
        server = self.start(scope)
        self.assertIn(REFUSED, self.send(server, "APPROVE")["error"])
        self.assertEqual(self.posts, [])
        self.assertTrue(self.send(server, "REQUEST_CHANGES")["ok"])
        dry = self.start(scope, no_write=True)
        self.assertIn(REFUSED, self.send(dry, "APPROVE")["error"])
        self.assertEqual(dry.recorded, [])
        self.assertTrue(self.send(dry, "REQUEST_CHANGES")["ok"])
        self.assertEqual([r["verdict"] for r in dry.recorded], ["REQUEST_CHANGES"])


class UnreadableViewRecord(Broker):
    """Arbiter on #97: an unreadable view record must not read as 'whole' (fail closed, named)."""

    def test_an_unreadable_ledger_refuses_the_approval_with_its_reason(self):
        scope = broker_ipc.RunScope(REPO, 7, HEAD, "reviewer", "fix-7", "run-x",
                                    str(self.root / "missing" / "runs.sqlite"), "g")
        server = self.start(scope)
        refused = self.send(server, "APPROVE")
        self.assertFalse(refused["ok"])
        self.assertIn(REFUSED, refused["error"])
        self.assertIn("could not read this run's view record (OperationalError)", refused["error"])
        self.assertEqual(self.posts, [])
        self.assertFalse(server.completed)


class FixerBroker(Broker):
    """A fixer that could not see the whole change cannot push it either (Arbiter on #97).

    The push is refused before the capability is spent and before any policy read or Git call;
    the fixer is told to publish its answers instead, and that one comment is its write.
    """

    PUSH_REFUSED = "a push is refused"

    def setUp(self):
        super().setUp()
        self.calls = []

    def api(self, loop, path, method="GET", body=None, login=None):
        self.calls.append((method, path, login))
        if method == "POST" and path.endswith("/issues/7/comments"):
            self.posts.append(body)
            return {"id": 23}
        return super().api(loop, path, method, body, login)

    def fixer_run(self, partial, push_on=True):
        # A policy-admitted fixer run: these tests are about the partial view, so the policy
        # must not also refuse the writes (#81's check is a separate layer, own tests).
        raw = {**self.loop, "unattended_fixer_push": True}
        self.loop = raw
        sup = Supervisor(self.root / "runs.sqlite")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("f", REPO, 7, HEAD, "fixer")
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='running', owner='w', launch_intent=1, "
                        "push_admitted=?", (1 if push_on else 0,))
            run_id = con.execute("SELECT id FROM runs").fetchone()[0]
        sup.record_view(run_id, "w", partial)
        scope = broker_ipc.RunScope(REPO, 7, HEAD, "fixer", "fix-7", run_id, str(sup.db), None,
                                    partial_view=partial)
        return sup, scope

    def raw(self, server, request):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(6)
            client.connect(str(server.socket_path))
            client.sendall(json.dumps(request).encode() + b"\n")
            client.shutdown(socket.SHUT_WR)
            return json.loads(client.recv(16384))

    def push(self, server):
        return self.raw(server, {"operation": "push", "manifest": {
            "files": [{"path": "src/lib.rs", "content": "fn x() {}\n"}], "message": "fix"}})

    def test_a_partial_view_refuses_the_push_and_publishes_the_answers_instead(self):
        sup, scope = self.fixer_run(REASON)
        server = self.start(scope, require_push=True)
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
             mock.patch("diaktoros.safe_push.push", side_effect=AssertionError("git push")):
            refused = self.push(server)
        self.assertFalse(refused["ok"])
        self.assertIn(REFUSED, refused["error"])
        self.assertIn(self.PUSH_REFUSED, refused["error"])
        self.assertIn("--answers-file", refused["error"])
        self.assertIn("HTTP 404", refused["error"])
        self.assertEqual(self.posts, [])
        self.assertFalse(server.completed)
        # The capability is unspent: the answers, with no push, are this turn's one write.
        answered = self.raw(server, {"operation": "request_review", "verdict": "",
                                     "body": "The file list was unavailable (404); from /work "
                                             "the finding at src/lib.rs:3 still holds."})
        self.assertTrue(answered["ok"], answered)
        self.assertEqual(answered["result"].get("answers"), "posted")
        self.assertEqual(len(self.posts), 1)
        self.assertIn("The file list was unavailable", self.posts[0]["body"])
        # No review request: nothing was pushed, so there is nothing new to review.
        self.assertFalse([c for c in self.calls if c[0] == "POST" and "requested_reviewers" in c[1]])
        self.assertTrue(server.completed)
        with ledger.connect(sup.db) as con:
            self.assertEqual(con.execute("SELECT state, base, head FROM fixer_answers").fetchone(),
                             ("posted", HEAD, HEAD))
        again = self.push(server)
        self.assertFalse(again["ok"])

    def test_a_whole_view_still_needs_a_push_before_answers(self):
        sup, scope = self.fixer_run("")
        server = self.start(scope, require_push=True)
        answered = self.raw(server, {"operation": "request_review", "verdict": "", "body": "x"})
        self.assertFalse(answered["ok"])
        self.assertIn("confirmed push first", answered["error"])
        self.assertEqual(self.posts, [])
        with self.assertRaises(ValueError):          # the ledger refuses a push-less record too
            sup.begin_answers(run_id=scope.run_id, repo=REPO, pr=7, base=HEAD, head=HEAD,
                              body="x")

    def test_the_fixer_is_told_to_answer_not_to_push(self):
        world = pc.World([pc.changed(1)])
        world.gone[pc.FILES] = "HTTP 404 {}"
        with mock.patch.object(gh, "fetch", side_effect=world.fetch):
            fixer = run_supervisor.pr_change(pc.Base.loop, {**pc.Base.row, "seat": "fixer"})
            reviewer = run_supervisor.pr_change(pc.Base.loop, pc.Base.row)
        self.assertIn("the broker refuses a push from this turn", fixer.record)
        self.assertIn("--answers-file", fixer.record)
        self.assertNotIn("do not approve", fixer.record)
        self.assertIn("do not approve it", reviewer.record)
        self.assertNotIn("refuses a push", reviewer.record)


class EveryLessThanWholeView(FixerBroker):
    """Arbiter on #97: a view truncated by DIFF_BYTES, or with an unnamed remainder past GitHub's
    listing, is also partial — recorded, and enforced like the unreadable list."""

    def truncated(self, seat="reviewer"):
        # His probe: 100 files whose patches together are ~2 MiB — about 50 fit in the diff.
        world = pc.World([pc.changed(i, patch="@@ -1 +1 @@\n+" + "x" * 20900) for i in range(100)])
        with mock.patch.object(gh, "fetch", side_effect=world.fetch):
            return run_supervisor.pr_change(self.loop, {**pc.Base.row, "seat": seat})

    def test_a_diff_truncated_by_its_byte_bound_is_partial_and_refused(self):
        change = self.truncated()
        self.assertIn("file(s) omitted: the diff is bounded", change.diff)
        self.assertLess(change.diff.count("diff --git"), 100)
        self.assertIn("did not fit", change.partial)
        self.assertIn("You cannot see the whole change: do not approve it", change.record)
        sup, scope = self.ledgered(change.partial)
        server = self.start(scope, require_receipt=True)
        refused = self.send(server, "APPROVE")
        self.assertIn(REFUSED, refused["error"])
        self.assertIn("did not fit", refused["error"])
        self.assertEqual(self.posts, [])

    def test_a_truncated_fixer_cannot_push(self):
        change = self.truncated("fixer")
        self.assertIn("the broker refuses a push from this turn", change.record)
        sup, scope = self.fixer_run(change.partial)
        server = self.start(scope, require_push=True)
        refused = self.push(server)
        self.assertIn(self.PUSH_REFUSED, refused["error"])
        self.assertEqual(self.posts, [])

    def test_a_whole_view_under_the_bound_stays_complete(self):
        world = pc.World([pc.changed(i) for i in range(100)])
        with mock.patch.object(gh, "fetch", side_effect=world.fetch):
            change = run_supervisor.pr_change(self.loop, pc.Base.row)
        self.assertEqual(change.partial, "")
        self.assertNotIn("You cannot see the whole change", change.record)


class UnnamedRemainder(pc.Base):
    def world(self, declared):
        world = pc.Record.over_cap(self)
        world.pr["changed_files"] = declared
        return world

    def test_trees_that_explain_the_whole_gap_are_complete(self):
        self.assertEqual(self.change(self.world(3004)).partial, "")   # 3000 listed + 4 named

    def test_an_unnamed_remainder_is_partial(self):
        change = self.change(self.world(3006))
        self.assertIn("2 changed file(s) are neither listed by GitHub nor named", change.partial)
        section = change.record.split("### Changed files GitHub does not list", 1)[1]
        self.assertIn("- added: src/new.rs", section)                   # the named part stays
        self.assertIn("You cannot see the whole change: do not approve it", section)


class HostRecordsTheView(pc.Base):
    """pr_change says whether the view is complete; the worker records it before launch."""

    def test_pr_change_names_what_the_seat_cannot_see(self):
        whole = self.change(pc.World([pc.changed(1)]))
        self.assertEqual(whole.partial, "")
        world = pc.World([pc.changed(1)])
        world.gone[pc.FILES] = "HTTP 404 {}"
        self.assertIn("file list could not be read", self.change(world).partial)
        self.assertIn("HTTP 404", self.change(world).partial)
        world = pc.World([pc.changed(i) for i in range(3)])
        world.pr["changed_files"] = 5                 # declared more than listed, trees unreadable
        world.trees = {"x": {}}
        world.fail.add(f"/repos/{REPO}/compare/{BASE}...{HEAD}?per_page=1")
        partial = self.change(world).partial
        self.assertIn("did not list every changed file", partial)

    def run_worker(self, world):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            root.chmod(0o700)
            runtime = root / "runtime.json"
            runtime.write_text("{}")
            runtime.chmod(0o600)
            sup = Supervisor(root / "ledger.sqlite", production_config=runtime, hermes_home=root)
            with mock.patch.object(sup, "_spawn"):
                sup.enqueue("d", REPO, 7, HEAD, "reviewer")
            with ledger.connect(sup.db) as con:
                con.execute("UPDATE runs SET state='launching', owner='w', generation='g'")
                run_id = con.execute("SELECT id FROM runs").fetchone()[0]
            settings = {k: str(root) for k in ("source", "venv", "runtime", "rust")}
            seen = {}

            def turn(loop, scope, **kwargs):
                with ledger.connect(sup.db) as con:
                    seen["ledger"] = con.execute("SELECT partial_view FROM runs").fetchone()[0]
                seen["scope"] = scope
                return 0
            with mock.patch("diaktoros.seat_model.load_runtime", return_value=settings), \
                 mock.patch("diaktoros.seat_model.resolve_seat"), \
                 mock.patch.object(config, "by_repo",
                                   return_value={**self.loop, "state_dir": str(root / "state")}), \
                 mock.patch.object(gh, "fetch", side_effect=world.fetch), \
                 mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
                 mock.patch.object(run_supervisor, "effective_reviews", return_value=[]), \
                 mock.patch.object(trusted_turn, "run_turn", side_effect=turn), \
                 mock.patch.object(sup, "recover"):
                sup._run_production(run_id, "w")
            views = run_supervisor.view_view(sup.db, REPO, 7)
        return seen, views

    def test_an_unreadable_file_list_is_recorded_before_launch(self):
        world = pc.World([pc.changed(1)])
        world.gone[pc.FILES] = "HTTP 404 {}"
        seen, views = self.run_worker(world)
        self.assertIn("HTTP 404", seen["ledger"])           # durable before the seat starts
        self.assertEqual(seen["scope"].partial_view, seen["ledger"])
        self.assertEqual([(v["seat"], v["head"]) for v in views], [("reviewer", HEAD)])
        self.assertIn("HTTP 404", views[0]["partial_view"])

    def test_a_whole_change_is_recorded_complete(self):
        seen, views = self.run_worker(pc.World([pc.changed(1)]))
        self.assertEqual((seen["ledger"], seen["scope"].partial_view), ("", ""))
        self.assertEqual(views, [])


class LaunchPathRefuses(pc.Base):
    """Arbiter on #97: the whole path composed — host pr_change -> ledger + scope -> real run_turn ->
    real RunBroker over its real socket -> real receipt ledger. Only GitHub REST, the sandbox
    process and the inference proxy are faked; the fake sandbox is the seat, talking to the
    broker with the real client."""

    def test_a_404_file_list_launches_a_turn_whose_approval_the_broker_refuses(self):
        from types import SimpleNamespace
        from diaktoros import contained, inference_proxy, trusted_fetch
        world = pc.World([pc.changed(1)])
        world.gone[pc.FILES] = "HTTP 404 {}"
        world.pr.update({"user": {"login": "fix"}, "state": "open", "draft": False})
        world.pr["head"]["repo"] = {"full_name": REPO}
        world.pr["base"]["repo"] = {"full_name": REPO}
        posts = []
        base_fetch = world.fetch

        def fetch(loop, path, method="GET", body=None, login=None):
            if path == "/user":
                return {"login": login, "id": {"read": 1, "review": 2, "fix": 3}[login]}, ""
            if method == "POST" and path == f"/repos/{REPO}/pulls/7/reviews":
                posts.append(body)
                return {"id": 19}, ""
            if path == f"/repos/{REPO}/pulls/7/reviews/19":
                state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}
                return {"id": 19, "state": state[posts[-1]["event"]], "commit_id": HEAD,
                        "user": {"id": 2, "login": "review"}}, ""
            if method != "GET":
                raise AssertionError(f"unexpected write {method} {path}")
            return base_fetch(loop, path, method, body, login)

        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            root = Path(directory)
            root.chmod(0o700)
            tokens = {}
            for login in ("read", "review", "fix"):
                (root / f"{login}.pat").write_text("DUMMY_" + login)
                (root / f"{login}.pat").chmod(0o600)
                tokens[login] = str(root / f"{login}.pat")
            loop = {**self.loop, "tokens": tokens, "state_dir": str(root / "state")}
            runtime = root / "runtime.json"
            runtime.write_text("{}")
            runtime.chmod(0o600)
            sup = Supervisor(root / "ledger.sqlite", production_config=runtime, hermes_home=root)
            with mock.patch.object(sup, "_spawn"):
                sup.enqueue("d", REPO, 7, HEAD, "reviewer")
            generation = review_receipt.generation_for(world.pr, loop, 7, HEAD)
            with ledger.connect(sup.db) as con:
                con.execute("UPDATE runs SET state='launching', owner='w', generation=?, "
                            "launch_intent=1", (generation,))
                run_id = con.execute("SELECT id FROM runs").fetchone()[0]
            for name in ("venv", "runtime", "rust"):
                (root / name).mkdir()
            settings = {"source": str(root), "venv": str(root / "venv"),
                        "runtime": str(root / "runtime"), "rust": str(root / "rust")}
            inference = SimpleNamespace(upstream="https://model.invalid", key="k", model="m",
                                        api_mode="chat_completions", proxy_model="m",
                                        client_identity="", credential_provider=lambda: None)
            seat = {}

            def sandbox(**kw):                       # the seat, inside the "sandbox"
                sock = str(Path(kw["broker_socket_dir"]) / "broker.sock")
                seat["query"] = Path(kw["query"]).read_text()
                seat["approve"] = broker_client.call("review", verdict="APPROVE",
                                                     body="looks fine\nNot verified: nothing", socket_path=sock)
                seat["changes"] = broker_client.call("review", verdict="REQUEST_CHANGES",
                                                     body="the file list was unavailable\nNot verified: nothing",
                                                     socket_path=sock)
                return subprocess.CompletedProcess([], 0, "", "")

            class Inference:
                def __init__(self, directory, *a, **k):
                    self.directory = directory

                def __enter__(self):
                    self.directory.mkdir()
                    return self

                def __exit__(self, *a):
                    return False

            def stage(_loop, **kw):
                kw["sandbox_root"].mkdir()
                return kw["sandbox_root"]
            with mock.patch("diaktoros.seat_model.load_runtime", return_value=settings), \
                 mock.patch("diaktoros.seat_model.resolve_seat", return_value=inference), \
                 mock.patch.object(config, "by_repo", return_value=loop), \
                 mock.patch.object(gh, "fetch", side_effect=fetch), \
                 mock.patch.object(run_supervisor, "effective_reviews", return_value=[]), \
                 mock.patch.object(trusted_turn, "_safe_code_snapshot",
                                   side_effect=lambda src, dst: dst.mkdir()), \
                 mock.patch.object(trusted_fetch, "stage", side_effect=stage), \
                 mock.patch.object(inference_proxy, "InferenceCapability", Inference), \
                 mock.patch.object(contained, "run", side_effect=sandbox), \
                 mock.patch.object(sup, "recover"):
                sup._run_production(run_id, "w")
            with ledger.connect(sup.db) as con:
                state = con.execute("SELECT state, error, partial_view FROM runs").fetchone()
                receipts = con.execute("SELECT state, verdict FROM review_receipts").fetchall()
        self.assertIn("could not read the PR's file list", seat["query"])
        self.assertFalse(seat["approve"]["ok"])
        self.assertIn(REFUSED, seat["approve"]["error"])
        self.assertIn("HTTP 404", seat["approve"]["error"])
        self.assertTrue(seat["changes"]["ok"], seat["changes"])
        self.assertEqual([p["event"] for p in posts], ["REQUEST_CHANGES"])
        self.assertEqual(receipts, [("confirmed", "CHANGES_REQUESTED")])
        self.assertEqual(state[:2], ("succeeded", None))
        self.assertIn("HTTP 404", state[2])


class ExplainShowsIt(unittest.TestCase):
    """``explain`` says the current head cannot be approved by the loop, and why."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        loops = home / "review-loops.d"
        loops.mkdir()
        loop = {"id": "widgets", "repo": REPO, "base": "main", "cap": 3,
                "fixers": ["dev"], "reviewers": ["reviewer"], "reviewer_seat": "reviewer",
                "seats": {"reviewer": {"profile": "r", "route": "review"},
                          "fixer": {"profile": "f", "route": "fix"}},
                "state_dir": str(home / "state"), "tokens": {}, "read_token": "reader",
                "host": "http://127.0.0.1:9"}
        (loops / "widgets.json").write_text(json.dumps(loop))
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(home),
                                           "DIAKTOROS_CONFIG_DIR": str(loops)})
        env.start()
        self.addCleanup(env.stop)
        self.db = home / "state" / "diaktoros-runs.sqlite"
        sup = Supervisor(self.db, fixture_mode=True, fixture_command=["true"])
        sup._spawn = lambda: None
        for delivery, head in (("old", "c" * 40), ("new", HEAD)):
            sup.submit(delivery, REPO, 7, head, "reviewer")
            with ledger.connect(self.db) as con:
                con.execute("UPDATE runs SET state='running', owner='w' WHERE delivery=?",
                            (delivery,))
            sup.record_view(sup.get(delivery)["id"], "w", REASON + " at " + delivery)
            with ledger.connect(self.db) as con:
                con.execute("UPDATE runs SET state='succeeded' WHERE delivery=?", (delivery,))

    def explain(self):
        import argparse
        import contextlib
        import io
        from diaktoros import cli, gate
        report = {"url": "u", "read_at": "t", "state_line": "s", "chain": {"status": "direct"},
                  "budget": "b", "seat": "x", "queue": "q", "inflight": "i", "escalation": "e",
                  "hooks": "h", "sweep": "w", "github": "g", "blockers": [], "next": {"action": "n"},
                  "head": HEAD}
        out = io.StringIO()
        import gc
        gc.collect()  # close setUp's ledger connections, so their WAL files are not "before"
        before = sorted(p.name for p in self.db.parent.iterdir())
        with mock.patch.object(gate, "explain_facts", return_value={}), \
             mock.patch.object(gate, "explain", return_value=report), \
             contextlib.redirect_stdout(out):
            self.assertEqual(cli.cmd_explain(argparse.Namespace(loop="widgets", pr=7)), 0)
        self.assertEqual(sorted(p.name for p in self.db.parent.iterdir()), before)
        return out.getvalue()

    def test_explain_names_the_partial_view_at_this_head_only(self):
        text = self.explain()
        views = [line for line in text.splitlines() if line.strip().startswith("view:")]
        self.assertEqual(len(views), 1, text)
        self.assertIn(f"reviewer #7 @ {HEAD[:7]} succeeded — could not see the whole change: "
                      f"{REASON} at new", views[0])
        self.assertIn("the broker refuses its approval", views[0])
        self.assertIn("review it by hand", views[0])


if __name__ == "__main__":
    unittest.main()
