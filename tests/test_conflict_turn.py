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
from diaktoros import (broker, broker_ipc, config, contained, gh, ledger,  # noqa: E402
                         run_supervisor, safe_push, trusted_turn)
from diaktoros import fixer_answers, wire  # noqa: E402
from diaktoros.run_supervisor import CONFLICT_KEY, Supervisor  # noqa: E402

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


class Sequence(Claim):
    """#1907, the sequence on #1875 (review of #1917, F3): a conflict turn queued on an unreviewed
    head; then the review requests changes and the fix turn fails. The conflict row does not go
    first: it waits at the claim, the sweep queues nothing new, and once the fix lands the old
    row is superseded and the merge is queued on the fixed head. No head is reviewed before the
    fix, so no round is spent on findings nobody answered. On main the conflict row is claimed."""

    CR = {"id": 5, "state": "CHANGES_REQUESTED", "commit_id": fg.HEAD,
          "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "reviewer"}}
    FIXED = "e" * 40

    def sup(self):
        self.set_push(True)
        settings = self.root / "runtime.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        return Supervisor(config.home() / "state" / "diaktoros-runs.sqlite",
                          production_config=settings, hermes_home=self.root / "home")

    def claim(self, sup, head, reviews):
        pr = {"number": 7, "state": "open", "draft": False, "head": {"sha": head}}
        with mock.patch.object(gh, "api", return_value=pr), \
             mock.patch.object(gh, "reviews", return_value=reviews):
            return sup._claim()

    def sweep(self, head, reviews):
        from diaktoros import gate, observer, watch_conflicts
        with mock.patch.object(watch_conflicts, "base_tip", return_value="c" * 40), \
             mock.patch.object(gh, "reviews", return_value=reviews), \
             mock.patch.object(gate, "enqueue_isolated", return_value="enqueued") as queued, \
             mock.patch.object(observer, "notify") as notified:
            watch_conflicts.act_on_conflicts(self.loop, self.st, [(7, head, "b" * 40, "fixer")], {})
        return queued, notified

    def test_a_failed_fix_is_not_overtaken_by_a_merge(self):
        sup = self.sup()
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("conflict", fg.REPO, 7, fg.HEAD, "fixer",           # before the verdict
                        turn_key=f"{CONFLICT_KEY}{BASE_SHA}", require_push_admission=True)
            sup.enqueue("fix", fg.REPO, 7, fg.HEAD, "fixer", require_push_admission=True)
        with ledger.connect(sup.db) as con:                              # the fix timed out
            con.execute("UPDATE runs SET state='failed', error='TimeoutExpired' WHERE delivery='fix'")
        self.assertIsNone(self.claim(sup, fg.HEAD, [self.CR]))
        self.assertEqual(sup.get("conflict")["state"], "pending")         # waits, not claimed
        queued, notified = self.sweep(fg.HEAD, [self.CR])
        queued.assert_not_called()                                         # and none queued again
        self.assertIn("hermes dk retry", notified.call_args.kwargs["next_turn"])
        # The fix is retried and lands: a new head, with no verdict yet.
        self.assertIsNone(self.claim(sup, self.FIXED, [self.CR]))
        self.assertEqual(sup.get("conflict")["state"], "cancelled")       # superseded by the push
        queued, _ = self.sweep(self.FIXED, [self.CR])
        self.assertEqual(queued.call_args.args[1:4], ("fixer", 7, self.FIXED))
        self.assertTrue(queued.call_args.kwargs["turn_key"].startswith(CONFLICT_KEY))

    def test_once_the_head_is_approved_the_waiting_row_is_claimed(self):
        sup = self.sup()
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("conflict", fg.REPO, 7, fg.HEAD, "fixer",
                        turn_key=f"{CONFLICT_KEY}{BASE_SHA}", require_push_admission=True)
        self.assertIsNone(self.claim(sup, fg.HEAD, [self.CR]))
        approved = {**self.CR, "id": 6, "state": "APPROVED", "submitted_at": "2026-01-02T00:00:00Z"}
        self.assertIsNotNone(self.claim(sup, fg.HEAD, [self.CR, approved]))
        self.assertEqual(sup.get("conflict")["state"], "claimed")


for _name in [n for n in dir(Claim) if n.startswith("test_")]:
    setattr(Sequence, _name, None)       # Claim's own tests run once, in Claim


class Worker(sm.Worker):
    def conflict_run(self, merged=None, merge_error=None, reviews=()):
        """run_seat with the row keyed as a conflict turn and the host merge mocked; ``reviews``:
        the PR's reviews when the launch reads them (#1907), by default none."""
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
                        "push_admitted=1, launch_intent=1 WHERE delivery='d-c'")
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
             mock.patch.object(gh, "reviews", return_value=list(reviews)) as read, \
             merge as merged_tree, \
             mock.patch.object(trusted_turn, "run_turn", side_effect=run_turn), \
             mock.patch.object(sup, "recover"):
            sup._run_production(run_id, "w")
        with ledger.connect(sup.db) as con:
            row = con.execute("SELECT state, error FROM runs WHERE id=?", (run_id,)).fetchone()
        return seen, row, read, merged_tree

    def test_the_turn_gets_the_merge_scope_the_export_and_the_conflict_prompt(self):
        seen, row, reviews, merged_tree = self.conflict_run()
        self.assertEqual(row[0], "succeeded")
        reviews.assert_called_once()        # no verdict is answered; read for #1907's check only
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

    def test_the_conflict_prompt_has_the_size_limit_rule_and_reports_in_its_summary(self):
        # #1448: the merge turn writes code and its always-run check says "publish anyway", so
        # it gets the size-limit rule too; it writes no answers and no PR description, so the
        # check's gaps go in its summary.
        self.loop = {**self.loop, "fixer_check": "python3 tests/file_sizes.py"}
        seen, row, _, _ = self.conflict_run()
        self.assertEqual(row[0], "succeeded")
        text = " ".join(seen["prompt"].split())
        self.assertIn("**Don't squeeze code under a size limit.**", text)
        self.assertIn("When a file is over its limit, still publish the resolution, and say in "
                      "your summary which file is over which limit.", text)
        self.assertIn("**publish anyway** and say in your summary exactly what", text)
        self.assertIn("If it cannot run in this sandbox, say so in your summary.", text)
        self.assertNotIn("answers or PR description", text)
        self.assertNotIn("the PR description", text)

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
                # #1154: a person's conflict is remembered for the head, so the watchdog stops
                # requeueing it on every merge; "nothing to resolve" is not a person's.
                from diaktoros import conflict_person, state as state_mod
                st = state_mod.state_for({**self.loop, "base": "main"})
                recorded = conflict_person.get(st, 7, sm.HEAD)
                if label == "clean now":
                    self.assertEqual(recorded, "")
                else:
                    self.assertIn(words.replace("conflict needs a person: ", ""), recorded)
                    # #1271: only the whole-file refusal lapses (a later base can fix it).
                    entry = st.kv_get(st.dir / conflict_person.FILE, {})["7"]
                    if label == "whole-file":
                        self.assertAlmostEqual(entry["until"] - entry["at"],
                                               conflict_person.WHOLE_FILE_RETRY_S)
                    else:
                        self.assertNotIn("until", entry)
                (st.dir / conflict_person.FILE).unlink(missing_ok=True)

    def test_a_change_request_since_the_claim_holds_the_launch_and_merges_nothing(self):
        # Review of #1917, F1: a verdict can land between the claim and the launch.
        cr = {"id": 5, "state": "CHANGES_REQUESTED", "commit_id": sm.HEAD,
              "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "rev"}}
        self.loop = {**self.loop, "reviewers": ["rev"], "cap": 3}
        seen, row, _, merged_tree = self.conflict_run(reviews=[cr])
        self.assertEqual(seen, {}, "no turn launched")
        merged_tree.assert_not_called()
        self.assertEqual(row[0], "waiting")
        self.assertIn("conflict turn waits: its changes-requested verdict is not answered", row[1])

    def test_a_git_or_network_failure_is_a_retry(self):
        from diaktoros import broker
        seen, row, _, _ = self.conflict_run(
            merge_error=broker.BrokerDenied("isolated Git transport failed"))
        self.assertEqual((seen, row[0]), ({}, "waiting"))


for _name in [n for n in dir(sm.Worker) if n.startswith("test_")]:
    setattr(Worker, _name, None)     # its own tests run in test_seat_models


class FailedFixSequence(Worker):
    """#1907's sequence on #1875, end to end (review of #1917, F3), through the real worker
    (claim, launch, run), sweep, enqueue and push, with GitHub, Git and the seat faked: a change
    request; the fix turn killed at its budget; main moves. The merge never goes first, so no
    head is reviewed before the fix: the operator's retry of the fix is claimed and pushes, the
    merge follows on its head, and the next verdict, the second, is of a head with the fix in
    it. On main the sweep queues the merge over the failed fix, the merged head is reviewed with
    the finding unanswered, and the round is spent."""

    FIXED, MERGED_HEAD = "e" * 40, "f" * 40

    def setUp(self):
        super().setUp()
        tokens = {login: self.key_file(login + ".pat", "DUMMY_SECRET_" + login)
                  for login in ("read", "review", "fix")}
        self.loop = {**self.loop, "tokens": tokens, "read_token": "read", "reviewer_seat": "review",
                     "fixers": ["fix"], "reviewers": ["review"], "cap": 3,
                     "seats": {"reviewer": {"profile": "rev", "login": "review"},
                               "fixer": {"profile": "fix", "login": "fix"}}}
        Path(self.loop["state_dir"]).mkdir(exist_ok=True)     # the push's audit lives there
        (self.root / "runtime.json").write_text(json.dumps(self.settings))
        (self.root / "runtime.json").chmod(0o600)
        self.sup = Supervisor(self.root / "ledger.sqlite", hermes_home=self.home,
                              production_config=self.root / "runtime.json")
        self.sup._spawn = mock.Mock()
        self.fake = tsp.FakeGitHub()
        self.reviews = [self.review(1, sm.HEAD)]
        self.pushed = []

    def review(self, rid, head):
        return {"id": rid, "state": "CHANGES_REQUESTED", "commit_id": head,
                "submitted_at": f"2026-01-0{rid}T00:00:00Z", "user": {"login": "review"}}

    def github(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(mock.patch.object(config, "by_repo", return_value=self.loop))
        stack.enter_context(mock.patch.object(gh, "api", side_effect=self.fake.api))
        stack.enter_context(mock.patch.object(gh, "reviews",
                                              side_effect=lambda *_a, **_k: list(self.reviews)))
        return stack

    def sweep(self, head):
        """The watchdog's conflict sweep, with main moved, and the real enqueue."""
        from diaktoros import gate, observer, state as state_mod, watch_conflicts
        with self.github(), mock.patch.object(gate, "isolated_supervisor", return_value=self.sup), \
             mock.patch.object(watch_conflicts, "base_tip", return_value="c" * 40), \
             mock.patch.object(observer, "notify") as notified:
            watch_conflicts.act_on_conflicts(self.loop, state_mod.state_for(self.loop),
                                             [(7, head, BASE_SHA, "fixer")], {})
        return notified

    def work(self, turn):
        """One pass of the worker: the real claim, launch and run; the seat's turn is
        ``turn(scope)``."""
        from diaktoros.run_supervisor import pr_facts, turn_prompts
        with self.github(), \
             mock.patch.object(safe_push, "merged_tree", return_value=MERGED), \
             mock.patch.object(turn_prompts, "isolated_prompt", return_value="PROMPT"), \
             mock.patch.object(pr_facts, "pr_change", return_value=pr_facts.PRChange("C", "D")), \
             mock.patch.object(trusted_turn, "run_turn",
                               side_effect=lambda _loop, scope, **_kw: turn(scope)), \
             mock.patch.object(self.sup, "recover"):
            self.sup._run_one()

    def seat_push(self, scope):
        """The seat's push as the broker makes it (``_push_under_intent``), for the run's scope."""
        self.sup.begin_push(scope.run_id, scope.repo, scope.number, scope.head)
        outcome, _ = self.push(scope.head, dict(scope.merge) if scope.merge else None)
        self.sup.confirm_push(scope.run_id, scope.repo, scope.number, scope.head)
        return 0 if outcome == "published" else 1

    def push(self, head, merge=None):
        """The real ``safe_push.push`` (GitHub and Git faked): ``(outcome or refusal, head)``."""
        def cas(loop, repo, branch, at, files, message, login, identity, *, before_push,
                merge=None, **_):
            new = self.MERGED_HEAD if merge else self.FIXED
            before_push(new)
            self.pushed.append(("merge" if merge else "fix", at))
            self.fake.branch_head = self.fake.pr_head = new
            return new
        try:
            with self.github(), mock.patch.object(safe_push, "_git_cas", side_effect=cas):
                outcome = safe_push.push(self.loop, repo="acme/widgets", number=7, head=head,
                                         role="fixer", branch="fix-7", merge=merge,
                                         manifest={**tsp.manifest(), "base_head": head})["outcome"]
        except broker.BrokerDenied as exc:
            outcome = str(exc)
        return outcome, self.fake.branch_head

    def state(self, delivery):
        return self.sup.get(delivery)["state"]

    def conflict_rows(self):
        with ledger.connect(self.sup.db) as con:
            return con.execute("SELECT head, state FROM runs WHERE turn_key LIKE 'conflict:%' "
                               "ORDER BY created").fetchall()

    def spent(self):
        from diaktoros import gate
        return len(gate.verdicts(self.reviews, self.loop))

    def test_a_merge_never_spends_the_round_a_failed_fix_owes(self):
        def killed(_scope):
            raise trusted_turn.TurnBudgetExceeded(["sandbox"], 1800, 30)
        with self.github():
            self.sup.enqueue("fix", "acme/widgets", 7, sm.HEAD, "fixer",
                             require_push_admission=True)
        self.work(killed)                                          # the fix turn times out
        self.assertEqual(self.state("fix"), "failed")
        self.assertIn("killed at the 1800s turn budget", self.sup.get("fix")["error"])
        notified = self.sweep(sm.HEAD)                             # main moved
        self.assertEqual(self.conflict_rows(), [], "no merge queued over the failed fix")
        self.assertIn("hermes dk retry", notified.call_args.kwargs["next_turn"])
        # A merge queued before the verdict (the race) waits at the claim, and a turn past it
        # can't push: the head stays the reviewed one, so no fresh review starts.
        with self.github():
            self.sup.enqueue("early", "acme/widgets", 7, sm.HEAD, "fixer",
                             turn_key=f"{CONFLICT_KEY}{BASE_SHA}", require_push_admission=True)
        self.work(mock.Mock(side_effect=AssertionError("the merge ran")))
        self.assertEqual(self.state("early"), "pending")
        refused, head = self.push(sm.HEAD, merge={"base_ref": "main", "base_sha": BASE_SHA,
                                                  "tree": TREE})
        self.assertIn("a merge waits for the fixer here", refused)
        self.assertEqual((head, self.pushed, self.spent()), (sm.HEAD, [], 1))
        # The operator retries the fix: it is claimed ahead of the merge, and pushes.
        with self.github():
            self.sup.retry(self.sup.get("fix")["id"])
        self.work(self.seat_push)
        self.assertEqual((self.state("fix"), self.state("early")), ("succeeded", "pending"))
        self.assertEqual(self.fake.branch_head, self.FIXED)
        # Then the merge, on the fix's head: the waiting row is superseded, a new one queued,
        # claimed and pushed with the host's merge scope. The fresh review is of the fix.
        self.sweep(self.FIXED)
        self.work(self.seat_push)
        self.assertEqual(self.conflict_rows(), [(sm.HEAD, "cancelled"), (self.FIXED, "succeeded")])
        self.assertEqual(self.pushed, [("fix", sm.HEAD), ("merge", self.FIXED)])
        self.assertEqual(self.fake.branch_head, self.MERGED_HEAD)
        self.reviews.append(self.review(2, self.MERGED_HEAD))
        self.assertEqual(self.spent(), 2)


for _name in [n for n in dir(Worker) if n.startswith("test_")]:
    setattr(FailedFixSequence, _name, None)     # Worker's own tests run once, in Worker


class Broker(unittest.TestCase):
    """The broker's push passes the host-owned merge scope, and only for a conflict turn."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                           "DIAKTOROS_CONFIG_DIR": str(self.home / "cfg")})
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
        with mock.patch("diaktoros.config.by_repo", return_value=loop), \
             mock.patch("diaktoros.safe_push._manifest"), \
             mock.patch("diaktoros.safe_push.push", return_value=result) as push:
            server._dispatch(json.dumps({"operation": "push", "manifest": manifest}).encode())
        return push.call_args.kwargs

    def test_a_conflict_turns_push_is_a_merge(self):
        merge = {"base_ref": "main", "base_sha": BASE_SHA, "tree": TREE}
        self.assertEqual(self.push(merge)["merge"], merge)

    def test_an_ordinary_push_is_unchanged(self):
        self.assertNotIn("merge", self.push(None))

    def answers(self, merge):
        """The comment the broker posts for this turn's summary, through its real recording,
        after its push was confirmed."""
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET push_confirmed=1 WHERE id=?", (self.row["id"],))
            con.execute("DELETE FROM fixer_answers WHERE run_id=?", (self.row["id"],))
        scope = broker_ipc.RunScope("acme/widgets", 7, sm.HEAD, "fixer", "fix-7", self.row["id"],
                                    str(self.db), merge=merge)
        loop = {"repo": "acme/widgets", "state_dir": str(self.home), "unattended_fixer_push": True}
        server = broker_ipc.RunBroker(loop, scope, self.home)
        with mock.patch.object(broker, "authorize", return_value="fix"), \
             mock.patch.object(broker, "post_fixer_answers", return_value=99) as post:
            self.assertEqual(server._publish_answers("d" * 40, "a.py: kept both sides."), "posted")
        return post.call_args.kwargs["text"]

    def test_a_conflict_turns_summary_is_headed_as_a_merge_not_as_answers(self):
        # #1907: on #1875 a conflict turn's summary went out as "Fixer's answers to the review of
        # ..." and said "no finding was a defect", while the review's P1 was never attempted.
        text = self.answers({"base_ref": "main", "base_sha": BASE_SHA, "tree": TREE})
        self.assertIn(f"**Fixer's merge of `main` at `{BASE_SHA[:12]}` into `{sm.HEAD[:12]}`**",
                      text)
        self.assertIn("A merge only: it answers no review finding", text)
        self.assertNotIn("answers to the review", text)
        self.assertTrue(text.startswith(f"{wire.ANSWERS_MARKER} run={self.row['id']} "))
        self.assertIn(f"**Fixer's answers to the review of `{sm.HEAD[:12]}`**", self.answers(None))

    def comment(self, merge):
        body = fixer_answers.answers_comment_body("F1.py: kept both sides.", head="d" * 40,
                                                  base=sm.HEAD, run_id="r1", merge=merge)
        return {"user": {"login": "fix"}, "body": body, "created_at": "2026-01-02T00:00:00Z"}

    def test_a_merge_summary_is_never_read_as_answers(self):
        # Review of #1917, F2: the reviewer's record and the finding-answer filter read the same
        # marked comment; a merge summary must be neither "answers" nor an answer to F1.
        from diaktoros import finding_answers
        from diaktoros.run_supervisor import pr_facts
        loop = {"seats": {"fixer": {"login": "fix"}}, "reviewers": ["rev"]}
        merge = {"base_ref": "main", "base_sha": BASE_SHA, "tree": TREE}
        parsed = [fixer_answers.parse_answers_comment(self.comment(m), loop) for m in (merge, None)]
        self.assertEqual([p["merge"] for p in parsed], [True, False])
        review = {"id": 5, "state": "CHANGES_REQUESTED", "commit_id": sm.HEAD,
                  "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "rev"}, "body": "F1"}
        record = pr_facts.pr_record(loop, {}, [review], [self.comment(merge)])
        self.assertIn(f"fixer's merge of the base into {sm.HEAD[:12]} (a merge only: it answers "
                      "no finding)", record)
        self.assertNotIn("fixer's answers to the verdict", record)
        self.assertIn("fixer's answers to the verdict",
                      pr_facts.pr_record(loop, {}, [review], [self.comment(None)]))
        for merge_or_not, want in ((merge, []), (None, ["the fixer"])):
            with self.subTest(merge=bool(merge_or_not)), \
                    mock.patch.object(gh, "issue_comments_read",
                                      return_value=([self.comment(merge_or_not)], "")), \
                    mock.patch("diaktoros.issue_facts.closing_numbers", return_value=[]), \
                    mock.patch("diaktoros.issue_facts.maintainers_among", return_value=[]):
                self.assertEqual([a["who"] for a in finding_answers._read_answers(loop, 7)], want)

    def test_the_conflict_prompt_says_the_turn_answers_no_finding(self):
        from diaktoros import prompts
        self.assertIn("Say nothing about the\n   review's findings: this turn answers none of them",
                      prompts.ISOLATED_CONFLICT)


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

    def test_a_merge_is_not_pushed_over_a_change_request_the_fixer_owes(self):
        # Review of #1917, F1: the verdict may land during the conflict turn itself.
        self.fake.branch_head = self.fake.pr_head = tsp.HEAD
        self.loop = {**self.loop, "cap": 3}
        with self.assertRaisesRegex(broker.BrokerDenied, "a merge waits for the fixer here"):
            self.push(self.review("CHANGES_REQUESTED"), self.MERGE)
        self.assertEqual(self.fake.branch_head, tsp.HEAD, "nothing was pushed")

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
