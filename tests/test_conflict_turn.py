"""#303 stage 3, part B: a loop PR that conflicts with its base gets a resolving fixer turn.

The watchdog queues it (harness: ``observer``); here, the rest of the path: the claim lets a
conflict row through without a verdict, the worker merges on the host and either hands a person
the conflict (whole-file, workflow changes, nothing left to resolve) or launches the turn with the
merge scope and the conflict prompt, ``run_turn`` stages the merged tree as the export, and the
broker turns the seat's push into a merge commit.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
import test_safe_push as tsp  # noqa: E402
import test_seat_models as sm  # noqa: E402
from review_loop import (broker, broker_ipc, config, contained, gh, ledger,  # noqa: E402
                         run_supervisor, safe_push, trusted_turn)
from review_loop.run_supervisor import CONFLICT_KEY, Supervisor  # noqa: E402

BASE_SHA = "b" * 40
TREE = "c" * 40
MERGED = {"tree": TREE, "conflicted": ["a.py"], "entries": [], "skipped": [], "archive": b"",
          "workflows": False, "sides": "### What the PR changed in these files\n+x = 2"}


class Claim(fg.Base):
    """A conflict row needs no verdict to be claimed; a plain fixer row still does."""

    def claim(self, turn_key: str):
        self.set_push(True)
        settings = self.root / "runtime.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        sup = Supervisor(config.home() / "state" / "diaktoros-runs.sqlite",
                         production_config=settings, hermes_home=self.root / "home")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d", fg.REPO, 7, fg.HEAD, "fixer", turn_key=turn_key,
                        require_push_admission=True)
        pr = {"number": 7, "state": "open", "draft": False, "head": {"sha": fg.HEAD}}
        with mock.patch.object(gh, "api", return_value=pr), \
             mock.patch.object(gh, "reviews", return_value=[]):
            return sup._claim(), sup.get("d")

    def test_a_conflict_row_is_claimed_without_a_change_request(self):
        claimed, row = self.claim(f"{CONFLICT_KEY}{BASE_SHA}")
        self.assertIsNotNone(claimed)
        self.assertEqual(row["state"], "claimed")

    def test_a_plain_fixer_row_still_waits_for_one(self):
        claimed, row = self.claim("")
        self.assertIsNone(claimed)
        self.assertEqual(row["state"], "pending")


class Worker(sm.Worker):
    def conflict_run(self, merged=None, merge_error=None):
        """run_seat with the row keyed as a conflict turn and the host merge mocked."""
        runtime = self.root / "runtime.json"
        runtime.write_text(json.dumps(self.settings))
        runtime.chmod(0o600)
        sup = Supervisor(self.root / "ledger.sqlite", production_config=runtime,
                         hermes_home=self.home)
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d-c", "acme/widgets", 7, sm.HEAD, "fixer",
                        turn_key=f"{CONFLICT_KEY}{BASE_SHA}")
        with ledger.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='launching', owner='w', generation='g', "
                        "push_admitted=1 WHERE delivery='d-c'")
            run_id = con.execute("SELECT id FROM runs WHERE delivery='d-c'").fetchone()[0]
        seen = {}

        def run_turn(_loop, scope, **kw):
            seen.update(kw, scope=scope)
            return 0
        loop = {**self.loop, "base": "main"}
        merge = (mock.patch.object(safe_push, "merged_tree", side_effect=merge_error)
                 if merge_error else
                 mock.patch.object(safe_push, "merged_tree", return_value=merged or MERGED))
        with mock.patch.object(config, "by_repo", return_value=loop), \
             mock.patch.object(gh, "api", return_value={"number": 7, "head": {"sha": sm.HEAD,
                                                                              "ref": "fix-7"}}), \
             mock.patch.object(gh, "reviews") as reviews, \
             merge as merged_tree, \
             mock.patch.object(trusted_turn, "run_turn", side_effect=run_turn), \
             mock.patch.object(sup, "recover"):
            sup._run_production(run_id, "w")
        with ledger.connect(sup.db) as con:
            row = con.execute("SELECT state, error FROM runs WHERE id=?", (run_id,)).fetchone()
        return seen, row, reviews, merged_tree

    def test_the_turn_gets_the_merge_scope_the_export_and_the_conflict_prompt(self):
        seen, row, reviews, merged_tree = self.conflict_run()
        self.assertEqual(row[0], "succeeded")
        reviews.assert_not_called()                                   # no verdict is answered
        self.assertEqual(merged_tree.call_args.kwargs["base_sha"], BASE_SHA)
        self.assertEqual(merged_tree.call_args.kwargs["branch"], "fix-7")
        self.assertEqual(seen["scope"].merge,
                         {"base_ref": "main", "base_sha": BASE_SHA, "tree": TREE})
        self.assertIs(seen["merged"], MERGED)
        prompt = seen["prompt"]
        self.assertIn("no longer merges into main", prompt)
        self.assertIn(f"(at `{BASE_SHA[:7]}`)", prompt)
        self.assertIn("- `a.py`", prompt)
        self.assertIn("What the PR changed", prompt)
        self.assertLess(prompt.index("Conflicted files"), prompt.index("What the PR changed"))

    def test_a_person_gets_it_when_the_loop_should_not_resolve_it(self):
        for label, kwargs, words in (
                ("whole-file", {"merge_error": safe_push.NeedsPerson(
                    "merge conflicts a person must resolve: 1 whole-file conflict(s)")},
                 "conflict needs a person: merge conflicts a person must resolve"),
                ("workflows", {"merged": {**MERGED, "workflows": True}},
                 "changed workflow files"),
                ("clean now", {"merged": {**MERGED, "conflicted": []}}, "nothing to resolve")):
            with self.subTest(label):
                seen, row, _, _ = self.conflict_run(**kwargs)
                self.assertEqual(seen, {}, "no turn launched")
                self.assertEqual(row[0], "failed")
                self.assertIn(words, row[1])

    def test_a_git_or_network_failure_is_a_retry(self):
        from review_loop import broker
        seen, row, _, _ = self.conflict_run(
            merge_error=broker.BrokerDenied("isolated Git transport failed"))
        self.assertEqual((seen, row[0]), ({}, "waiting"))


for _name in [n for n in dir(sm.Worker) if n.startswith("test_")]:
    setattr(Worker, _name, None)     # its own tests run in test_seat_models


class Broker(unittest.TestCase):
    """The broker's push passes the host-owned merge scope, and only for a conflict turn."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                           "REVIEW_LOOP_CONFIG_DIR": str(self.home / "cfg")})
        env.start()
        self.addCleanup(env.stop)
        (self.home / "state").mkdir()
        self.db = self.home / "state" / "diaktoros-runs.sqlite"
        self.sup = Supervisor(self.db)
        self.sup.enqueue("fix", "acme/widgets", 7, sm.HEAD, "fixer")
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state='running',owner='worker',launch_intent=1,"
                        "push_admitted=1 WHERE delivery='fix'")
        self.row = self.sup.get("fix")

    def push(self, merge):
        scope = broker_ipc.RunScope("acme/widgets", 7, sm.HEAD, "fixer", "fix-7", self.row["id"],
                                    str(self.db), merge=merge)
        loop = {"repo": "acme/widgets", "state_dir": str(self.home), "unattended_fixer_push": True}
        server = broker_ipc.RunBroker(loop, scope, self.home)
        manifest = {"base_head": sm.HEAD, "message": "merge", "files": []}
        result = {"new_head": "d" * 40}
        with mock.patch("review_loop.config.by_repo", return_value=loop), \
             mock.patch("review_loop.safe_push._manifest"), \
             mock.patch("review_loop.safe_push.push", return_value=result) as push:
            server._dispatch(json.dumps({"operation": "push", "manifest": manifest}).encode())
        return push.call_args.kwargs

    def test_a_conflict_turns_push_is_a_merge(self):
        merge = {"base_ref": "main", "base_sha": BASE_SHA, "tree": TREE}
        self.assertEqual(self.push(merge)["merge"], merge)

    def test_an_ordinary_push_is_unchanged(self):
        self.assertNotIn("merge", self.push(None))


class MergeAuthorization(unittest.TestCase):
    """The real ``safe_push.push`` and ``broker.authorize``, GitHub reads and Git faked: a merge
    push needs no changes-requested verdict (an approved or unreviewed PR is its main case); an
    ordinary push still does."""

    MERGE = {"base_ref": "main", "base_sha": BASE_SHA, "tree": TREE}

    setUp = tsp.SafePushTests.setUp

    def cas(self, loop, repo, branch, head, files, message, login, identity, *, before_push,
            merge=None, **extra):
        self.merged = merge
        before_push(tsp.NEW_HEAD)
        self.fake.branch_head = self.fake.pr_head = tsp.NEW_HEAD
        return tsp.NEW_HEAD

    def push(self, reviews, merge):
        with mock.patch.object(gh, "reviews", return_value=reviews):
            return safe_push.push(self.loop, repo=tsp.REPO, number=7, head=tsp.HEAD,
                                  role="fixer", branch="fix-7", manifest=tsp.manifest(),
                                  merge=merge)

    def review(self, state):
        return [{"id": 41, "state": state, "commit_id": tsp.HEAD,
                 "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "review"}}]

    def test_a_merge_push_on_an_approved_or_unreviewed_pr_publishes(self):
        for reviews in (self.review("APPROVED"), []):
            with self.subTest(reviews=reviews):
                self.fake.branch_head = self.fake.pr_head = tsp.HEAD
                receipt = self.push(reviews, self.MERGE)
                self.assertEqual(receipt["outcome"], "published")
                self.assertEqual(self.merged, self.MERGE)

    def test_an_ordinary_push_without_a_change_request_is_still_refused(self):
        for reviews in (self.review("APPROVED"), []):
            with self.subTest(reviews=reviews), \
                    self.assertRaisesRegex(broker.BrokerDenied, "fixer verdict no longer current"):
                self.push(reviews, None)


class Staging(unittest.TestCase):
    """run_turn writes the merged tree as the export, verified, and only for its own scope."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name in ("venv", "runtime", "rust"):
            (self.root / name).mkdir()
        data = b"<<<<<<< ours\nx = 2\n=======\nx = 3\n>>>>>>> theirs\n"
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("merge/a.py")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        oid = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        self.merged = {**MERGED, "archive": buf.getvalue(), "entries": [("a.py", oid, len(data), False)]}
        self.loop = {"id": "w", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "state_dir": str(self.root / "state"), "fixers": ["fix"],
                     "reviewers": ["rev"], "tokens": {}, "read_token": "read",
                     "reviewer_seat": "rev", "seats": {"reviewer": {"login": "rev"},
                                                       "fixer": {"login": "fix"}}}

    def turn(self, scope_tree: str):
        seen = {}

        def run(**kw):
            seen["files"] = sorted(p.name for p in Path(kw["checkout"]).rglob("*") if p.is_file())
            seen["text"] = (Path(kw["checkout"]) / "a.py").read_text()
            return subprocess.CompletedProcess([], 0, "", "")

        class Inference:
            def __init__(self, directory, *a, **k):
                self.directory = directory

            def __enter__(self):
                self.directory.mkdir()
                return self

            def __exit__(self, *a):
                return False
        scope = broker_ipc.RunScope("acme/widgets", 7, sm.HEAD, "fixer", "fix-7", "rid",
                                    str(self.root / "runs.sqlite"),
                                    merge={"base_ref": "main", "base_sha": BASE_SHA,
                                           "tree": scope_tree})
        with mock.patch.object(trusted_turn, "_safe_code_snapshot",
                               side_effect=lambda src, dst: dst.mkdir()), \
             mock.patch.object(trusted_turn.trusted_fetch, "stage",
                               side_effect=AssertionError("a conflict turn never stages the head")), \
             mock.patch.object(trusted_turn.deps, "prepare", return_value=[]), \
             mock.patch.object(trusted_turn.inference_proxy, "InferenceCapability", Inference), \
             mock.patch.object(contained, "run", side_effect=run):
            try:
                trusted_turn.run_turn(self.loop, scope, source=self.root, venv=self.root / "venv",
                                      runtime=self.root / "runtime", rust=self.root / "rust",
                                      upstream="https://model.invalid", key="k", model="m",
                                      prompt="P", timeout=5, work_root=self.root / "work",
                                      merged=self.merged)
            except trusted_turn.TurnDenied as exc:
                seen["denied"] = str(exc)
        return seen

    def test_the_export_is_the_merged_tree_with_its_markers(self):
        seen = self.turn(TREE)
        self.assertEqual(seen["files"], ["a.py"])
        self.assertIn("<<<<<<< ours", seen["text"])

    def test_an_archive_for_another_merge_is_refused(self):
        seen = self.turn("e" * 40)
        self.assertNotIn("files", seen)
        self.assertIn("does not match the turn scope", seen["denied"])


if __name__ == "__main__":
    unittest.main()
