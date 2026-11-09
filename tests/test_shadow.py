"""Shadow reviews (#218): a second reviewer model in no-write mode, beside the loop and never in it.

The shadow seat is queued beside a sampled reviewer turn, runs the reviewer's prompt through its
own profile with a no-write broker, and records what it would have submitted. It never occupies
the PR, never claims a seat or marks a head in flight, never pages the operator and never counts
as one of the PR's turns.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import ci, config, gate, gh, merge_check, ready, run_supervisor, seat_model, stats  # noqa: E402
from diaktoros import state as state_mod, trusted_turn  # noqa: E402
from diaktoros.run_supervisor import claims  # noqa: E402
from diaktoros.run_supervisor import views  # noqa: E402
from diaktoros.run_supervisor import Supervisor, pr_facts, turn_prompts  # noqa: E402

HEAD = "a" * 40
GREEN = ci.CIState(passed=["tests"])
RAW = {"id": "widgets", "repo": "acme/widgets", "fixers": ["fixer"], "reviewers": ["reviewer"],
       "read_token": "reader",
       "seats": {"reviewer": {"profile": "rev", "route": "r"},
                 "fixer": {"profile": "fix", "route": "f"}}}


class Config(unittest.TestCase):
    def test_off_unless_a_profile_is_set(self):
        loop = config.normalize(dict(RAW))
        self.assertEqual((loop["shadow"], config.shadow_profile(loop)), ({}, ""))
        self.assertEqual(config.seat_profile(loop, "shadow"), "")

    def test_profile_every_and_seat_limits(self):
        loop = config.normalize({**RAW, "shadow": {"profile": "grok", "every": 3},
                                 "seats": {**RAW["seats"], "shadow": {"daily_turns": 4}}})
        self.assertEqual(config.seat_profile(loop, "shadow"), "grok")
        self.assertEqual(config.shadow_every(loop), 3)
        self.assertEqual(config.seat_daily_turns(loop, "shadow"), 4)
        # A shadow never inherits the parallelism the operator chose for real reviews.
        self.assertEqual(config.seat_concurrency({"concurrency": 3, "seats": {}}, "shadow"), 1)
        self.assertEqual(config.seat_concurrency({"concurrency": 3, "seats": {}}, "reviewer"), 3)
        self.assertEqual(config.shadow_every(config.normalize(
            {**RAW, "shadow": {"profile": "grok"}})), config.DEFAULT_SHADOW_EVERY)

    def test_a_bad_block_is_refused(self):
        for bad in ({"every": 2}, {"profile": "a/b"}, {"profile": "g", "every": 0},
                    {"profile": "g", "every": True}, {"profile": "g", "route": "x"},
                    "grok"):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.normalize({**RAW, "shadow": bad})
        with self.assertRaises(config.ConfigError):
            config.normalize({**RAW, "seats": {**RAW["seats"], "shadow": {"login": "x"}}})


class Doctor(unittest.TestCase):
    def checks(self, shadow):
        from diaktoros.doctor import seat_checks
        loop = config.normalize({**RAW, "shadow": {"profile": shadow}})
        with mock.patch.object(seat_checks, "runtime_settings", return_value=({}, None)), \
                mock.patch.object(seat_model, "describe_seat",
                                  return_value=("ok", "described", "")):
            return {c.name: c for c in seat_checks.check_seat_models(loop)}

    def test_the_shadow_seats_model_is_checked_and_must_be_its_own_profile(self):
        from diaktoros import doctor
        got = self.checks("grok")
        self.assertIn("model:shadow", got)
        self.assertNotIn("model:shadow-independent", got)
        same = self.checks("rev")                       # the reviewer's own profile
        self.assertEqual(same["model:shadow-independent"].status, doctor.ABSENT)


class Enqueue(unittest.TestCase):
    def loop(self, **shadow):
        return config.normalize({**RAW, "shadow": shadow} if shadow else dict(RAW))

    def test_a_sampled_reviewer_turn_queues_one_shadow_beside_it(self):
        sup = mock.Mock()
        sup.submit.return_value = "enqueued"
        loop = self.loop(profile="grok", every=1)
        with mock.patch.object(gate, "isolated_supervisor", return_value=sup):
            self.assertEqual(gate.enqueue_isolated(loop, "reviewer", 8, HEAD), "enqueued")
        seats = [call.args[4] for call in sup.submit.call_args_list]
        self.assertEqual(seats, ["reviewer", "shadow"])
        self.assertEqual(sup.submit.call_args_list[1].args[0], f"acme/widgets:8:{HEAD}:shadow")

    def test_no_profile_or_no_sample_or_another_seat_queues_none(self):
        sup = mock.Mock()
        sup.submit.return_value = "enqueued"
        cases = [(self.loop(), "reviewer"), (self.loop(profile="grok", every=1), "fixer")]
        for loop, seat in cases:
            sup.submit.reset_mock()
            with self.subTest(seat=seat), mock.patch.object(gate, "isolated_supervisor",
                                                            return_value=sup):
                gate.enqueue_isolated(loop, seat, 8, HEAD)
                self.assertEqual([c.args[4] for c in sup.submit.call_args_list], [seat])
        loop = self.loop(profile="grok", every=1000)
        heads = [f"{i:040x}" for i in range(40)]
        picked = [h for h in heads if gate.shadow_sampled(loop, 8, h)]
        self.assertLess(len(picked), 5)
        # The choice is the same on every redelivery.
        self.assertEqual(picked, [h for h in heads if gate.shadow_sampled(loop, 8, h)])

    def test_a_duplicate_reviewer_delivery_queues_no_second_shadow(self):
        sup = mock.Mock()
        sup.submit.return_value = "duplicate pending"
        with mock.patch.object(gate, "isolated_supervisor", return_value=sup):
            gate.enqueue_isolated(self.loop(profile="grok", every=1), "reviewer", 8, HEAD)
        self.assertEqual([c.args[4] for c in sup.submit.call_args_list], ["reviewer"])

    def test_a_failing_shadow_enqueue_never_changes_the_reviewers_answer(self):
        sup = mock.Mock()
        sup.submit.side_effect = ["enqueued", OSError("disk full")]
        with mock.patch.object(gate, "isolated_supervisor", return_value=sup):
            self.assertEqual(gate.enqueue_isolated(self.loop(profile="grok", every=1),
                                                   "reviewer", 8, HEAD), "enqueued")


class Ledger(unittest.TestCase):
    """The ledger side: occupancy, notices, retry/escalate and stats never count a shadow."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.db = pathlib.Path(temp.name) / "runs.sqlite"
        self.sup = Supervisor(self.db, fixture_mode=True)
        self.sup._spawn = lambda: None

    def add(self, seat, state="pending", pr=8):
        delivery = f"acme/widgets:{pr}:{HEAD}:{seat}"
        self.sup.enqueue(delivery, "acme/widgets", pr, HEAD, seat)
        if state != "pending":
            with closing(sqlite3.connect(self.db)) as con, con:
                con.execute("UPDATE runs SET state=?, owner='o', lease=? WHERE delivery=?",
                            (state, time.time() + 600, delivery))
        return self.sup.get(delivery)

    def test_a_running_shadow_never_blocks_the_prs_real_turns(self):
        self.add("shadow", "running")
        fixer = self.add("fixer")
        claimed = self.sup._claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], fixer["id"])

    def test_a_running_reviewer_never_blocks_the_shadow(self):
        self.add("reviewer", "running")
        shadow = self.add("shadow")
        claimed = self.sup._claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0], shadow["id"])

    def test_a_real_turn_still_blocks_another_real_turn(self):
        self.add("reviewer", "running")
        self.add("fixer")
        self.assertIsNone(self.sup._claim())

    def test_a_failed_shadow_pages_nobody(self):
        self.add("shadow", "failed")
        delivered = []
        self.sup.notify(lambda message: delivered.append(message))
        self.assertEqual(delivered, [])
        self.add("reviewer", "failed", pr=9)
        self.sup.notify(lambda message: delivered.append(message))
        self.assertEqual(len(delivered), 1)

    def test_record_shadow_is_owner_scoped_once_and_shadow_only(self):
        shadow = self.add("shadow", "running")
        reviewer = self.add("reviewer", "running", pr=9)
        entry = {"verdict": "REQUEST_CHANGES", "body": "F1: a.py:1: no test", "authorized": True}
        self.assertFalse(self.sup.record_shadow(shadow["id"], "someone-else", entry))
        self.assertFalse(self.sup.record_shadow(reviewer["id"], "o", entry))
        self.assertFalse(self.sup.record_shadow(shadow["id"], "o", {"verdict": "COMMENT",
                                                                    "body": "x"}))
        self.assertTrue(self.sup.record_shadow(shadow["id"], "o", entry))
        self.assertFalse(self.sup.record_shadow(shadow["id"], "o", entry))
        got = self.sup.shadow_reviews("acme/widgets", 8)
        self.assertEqual([(r["verdict"], r["authorized"], r["held"]) for r in got],
                         [("REQUEST_CHANGES", 1, None)])
        self.assertEqual(self.sup.shadow_reviews("acme/widgets", 9), [])

    def test_stats_live_view_leaves_shadow_out(self):
        self.add("shadow", "running")
        self.add("reviewer", "running", pr=9)
        now = stats.now(self.db, "acme/widgets")
        self.assertEqual([r["seat"] for r in now["runs"]], ["reviewer"])


class Reports(Ledger):
    def test_turn_counts_by_model_and_revision_leave_shadow_out(self):
        self.add("shadow")
        self.add("reviewer", pr=9)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("UPDATE runs SET model='m-'||seat, prompt_rev='p'")
        revisions = stats.revisions(self.db, "acme/widgets", 0)
        self.assertEqual(list(revisions["model"]), ["m-reviewer"])
        self.assertEqual(revisions["prompt_rev"]["p"]["turns"], 1)
        window = stats.review_window(self.db, "acme/widgets", 0, time.time() + 10)
        self.assertEqual(list(window["models"]), ["m-reviewer"])


class Compare(Ledger):
    """Part 2 (#218): each shadow review set beside the real review at the same head."""

    def shadow_at(self, pr, head, verdict, body, model="grok-x"):
        run = self.add_at("shadow", pr, head)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO shadow_reviews(run_id,repo,pr,head,model,verdict,body,"
                        "authorized,created) VALUES(?,?,?,?,?,?,?,1,?)",
                        (run, "acme/widgets", pr, head, model, verdict, body, time.time()))

    def real_at(self, pr, head, verdict):
        run = self.add_at("reviewer", pr, head)
        with closing(sqlite3.connect(self.db)) as con, con:
            con.execute("INSERT INTO review_receipts(run_id,state,generation,principal_id,"
                        "review_id,verdict,created) VALUES(?,'posted','g',1,1,?,?)",
                        (run, verdict, time.time()))

    def add_at(self, seat, pr, head):
        delivery = f"acme/widgets:{pr}:{head}:{seat}"
        self.sup.enqueue(delivery, "acme/widgets", pr, head, seat)
        return self.sup.get(delivery)["id"]

    def outcomes(self, tracked=None):
        return stats.shadow_outcomes(self.db, "acme/widgets", 0, time.time() + 10, tracked or {})

    def test_agreement_disagreement_both_ways_pending_and_findings(self):
        b, c, d, e = "b" * 40, "c" * 40, "d" * 40, "e" * 40
        # #8: both block; the shadow cites a.py:10, the reviewer a.py:14 (near enough) and z.py.
        self.shadow_at(8, b, "REQUEST_CHANGES", "F1: a.py:10: no test\nF2: q.py: typo")
        self.real_at(8, b, "REQUEST_CHANGES")
        # #9: the shadow blocks what the reviewer approved.
        self.shadow_at(9, c, "REQUEST_CHANGES", "F1: m.py:3: leaks the token")
        self.real_at(9, c, "APPROVE")
        # #10: the shadow approves what the reviewer blocked.
        self.shadow_at(10, d, "APPROVE", "looks fine")
        self.real_at(10, d, "REQUEST_CHANGES")
        # #11: no real verdict at this head yet (one at another head does not count).
        self.shadow_at(11, e, "APPROVE", "ok")
        self.real_at(11, "f" * 40, "REQUEST_CHANGES")
        tracked = {"8": {"findings": {"F1": {"text": "a.py:14: missing test", "head": b},
                                      "F2": {"text": "z.py:1: unused", "head": b},
                                      "F3": {"text": "a.py:10: older", "head": "0" * 40}}},
                   "10": {"findings": {"F1": {"text": "k.py:2: off by one", "head": d}}}}
        got = self.outcomes(tracked)
        self.assertEqual((got["shadowed"], got["paired"], got["agreed"]), (4, 3, 1))
        self.assertEqual(got["models"], {"grok-x": {"shadowed": 4, "paired": 3, "agreed": 1}})
        self.assertEqual([c["pr"] for c in got["blocked_approved"]], [9])
        self.assertEqual(got["blocked_approved"][0]["findings"], ["m.py:3: leaks the token"])
        self.assertEqual([c["pr"] for c in got["approved_blocked"]], [10])
        self.assertEqual(got["approved_blocked"][0]["findings"], ["k.py:2: off by one"])
        self.assertEqual(got["findings"], {"matched": 1, "shadow_only": 2, "reviewer_only": 2})
        text = stats.report_text({**stats.report({"repo": "acme/widgets", "id": "w"}, self.db,
                                                 0, time.time() + 10, breaches={},
                                                 pr_state=lambda n: None, tracked=tracked)})
        self.assertIn("shadow reviews: 4 (3 beside a real verdict) · verdicts agreed 1/3", text)
        self.assertIn("shadow blocked, reviewer approved: #9 at ccccccc (m.py:3: leaks the token)",
                      text)
        self.assertIn("shadow approved, reviewer blocked: #10 at ddddddd", text)

    def test_no_shadow_reviews_shows_nothing(self):
        self.assertEqual(self.outcomes(), {})
        text = stats.report_text(stats.report({"repo": "acme/widgets", "id": "w"}, self.db, 0,
                                              time.time() + 10, breaches={},
                                              pr_state=lambda n: None, tracked={}))
        # No shadow-review section or case lines. The escalations line always names its
        # `by shadow` bucket (#1116), so that one line may carry the word, and no other.
        self.assertEqual([line for line in text.splitlines()
                          if "shadow" in line and not line.startswith("escalations: ")], [])
        self.assertEqual(text.count("shadow"), 1)
        self.assertIsNone(stats.shadow_outcomes(self.db.parent / "none.sqlite", "acme/widgets",
                                                0, 1, {}))

    def test_citations_match_by_file_and_nearby_line(self):
        self.assertEqual(stats._match_findings(["a.py:10: x", "b.py: y", "c.py:5: v"],
                                               ["a.py:40: z", "./b.py:99: w", "c.py:15: u"]),
                         (2, 1, 1))
        self.assertEqual(stats._match_findings(["a.py:10: x"], ["a.py: no line"]), (1, 0, 0))
        self.assertEqual(stats._match_findings(["no citation"], ["a.py:1: x"]), (0, 1, 1))


class Display(Compare):
    """Part 3 (#218): `explain` and the Desktop drawer show a PR's shadow reviews."""

    def test_one_prs_shadows_beside_the_real_verdict(self):
        b, c = "b" * 40, "c" * 40
        self.shadow_at(8, b, "REQUEST_CHANGES",
                       "F1: a.py:3: leaks\nF2: b.py: x\nF3: c.py: y\nF4: d.py: z")
        self.real_at(8, b, "APPROVE")
        self.shadow_at(8, c, "APPROVE", "fine")
        self.shadow_at(9, c, "APPROVE", "other PR")
        got, older = stats.shadow_reviews_for(self.db, "acme/widgets", 8)
        self.assertEqual(older, 0)
        self.assertEqual([(g["head"], g["verdict"], g["real"], g["agrees"]) for g in got],
                         [("ccccccc", "approved", None, None), ("bbbbbbb", "changes", "approved", False)])
        self.assertEqual((got[1]["findings"], got[1]["more"]),
                         (["a.py:3: leaks", "b.py: x", "c.py: y"], 1))
        self.assertEqual(stats.shadow_reviews_for(self.db.parent / "none", "acme/widgets", 8),
                         ([], 0))

    def test_older_reviews_past_the_cap_are_counted(self):
        for i in range(stats.SHADOW_SHOWN + 2):
            self.shadow_at(8, f"{i:040x}", "APPROVE", "ok")
        got, older = stats.shadow_reviews_for(self.db, "acme/widgets", 8)
        self.assertEqual((len(got), older), (stats.SHADOW_SHOWN, 2))

    def test_a_pending_shadow_is_not_in_the_prs_queue(self):
        self.add("shadow")
        self.add("reviewer", pr=9)
        self.assertEqual([r["seat"] for r in views.pending_view(self.db, "acme/widgets")],
                         ["reviewer"])

    def test_a_failed_shadow_is_not_a_run_to_act_on(self):
        self.add("shadow", "failed")
        self.add("reviewer", "failed", pr=9)
        with closing(sqlite3.connect(self.db)) as con:
            con.row_factory = sqlite3.Row
            seats = [r["seat"] for r in views.runs_view(con, "acme/widgets")]
        self.assertEqual(seats, ["reviewer"])

    def test_explain_prints_them_and_the_drawer_carries_them(self):
        import contextlib
        import io
        from diaktoros import desktop_api
        from diaktoros.cli import operations
        b = "b" * 40
        self.shadow_at(8, b, "REQUEST_CHANGES", "F1: a.py:3: leaks the token")
        self.real_at(8, b, "APPROVE")
        out = io.StringIO()
        with mock.patch.object(operations, "_ledger_path", return_value=self.db), \
                contextlib.redirect_stdout(out):
            operations._print_shadows({"repo": "acme/widgets"}, 8, "  shadow:     ")
        self.assertIn("grok-x at bbbbbbb would have changes · reviewer approved (disagrees) · "
                      "recorded, never posted", out.getvalue())
        self.assertIn("| a.py:3: leaks the token", out.getvalue())
        loop = {"id": "w", "repo": "acme/widgets", "cap": 3}
        with mock.patch.object(desktop_api, "_loop", return_value=loop), \
                mock.patch.object(run_supervisor, "production_ledger", return_value=self.db), \
                mock.patch.object(desktop_api, "_findings", return_value=[]), \
                mock.patch.object(desktop_api, "_decisions", return_value=[]), \
                mock.patch.object(desktop_api, "_breach", return_value=None), \
                mock.patch.object(desktop_api.pacing, "seats", return_value={}):
            view = desktop_api.pr_view("w", 8)
        self.assertEqual([(s["verdict"], s["real"]) for s in view["shadows"]],
                         [("changes", "approved")])


class ProductionTurn(unittest.TestCase):
    """The real ``_run_production`` for a shadow row, its turn faked."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = pathlib.Path(temp.name)
        runtime = self.home / "diaktoros-runtime.json"
        runtime.write_text(json.dumps({"source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}))
        runtime.chmod(0o600)
        patch = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home),
                                             "DIAKTOROS_CONFIG_DIR": str(self.home / "none")})
        patch.start()
        self.addCleanup(patch.stop)
        self.loop = config.normalize({**RAW, "state_dir": str(self.home / "state"),
                                      "shadow": {"profile": "grok", "every": 1}})
        self.inference = mock.Mock(upstream="https://api.x.ai/v1", provider="xai", profile="grok",
                                   key="k", model="grok-x", api_mode="chat_completions",
                                   proxy_model="", client_identity="")

    def run_shadow(self, submissions, reviews=(), rc=0, checks=GREEN, pr=None, created=None,
                   reviewer=("confirmed", "APPROVED"), reviewer_state="succeeded"):
        sup = self.queue_shadow()
        self.reviewer_is(sup, reviewer, reviewer_state)
        return self.claim_shadow(sup, submissions, reviews, rc, checks, pr, created)

    def queue_shadow(self):
        with mock.patch.object(Supervisor, "_spawn"), \
                mock.patch.object(config, "by_repo", return_value=self.loop):
            gate.enqueue_isolated(self.loop, "reviewer", 8, HEAD)
        sup = Supervisor(self.home / "state" / "diaktoros-runs.sqlite",
                         production_config=self.home / "diaktoros-runtime.json",
                         hermes_home=self.home)
        self.assertIsNotNone(sup.get(f"acme/widgets:8:{HEAD}:shadow"),
                             "the sampled reviewer turn queued a shadow")
        return sup

    def reviewer_is(self, sup, reviewer, reviewer_state, confirmed=None):
        """#1647: the shadow runs once its reviewer turn's APPROVE is confirmed (``reviewer``:
        that turn's receipt, or None for none yet; ``confirmed``: when, default now)."""
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET state=? WHERE seat='reviewer'", (reviewer_state,))
            if reviewer is not None:
                rid = sup.get(f"acme/widgets:8:{HEAD}:reviewer")["id"]
                con.execute("INSERT OR REPLACE INTO review_receipts(run_id,state,generation,"
                            "principal_id,"
                            "review_id,verdict,created,confirmed) VALUES(?,?,'g',1,9,?,?,?)",
                            (rid, reviewer[0], reviewer[1], time.time(),
                             time.time() if confirmed is None else confirmed))

    def claim_shadow(self, sup, submissions, reviews=(), rc=0, checks=GREEN, pr=None,
                     created=None):
        row = sup.get(f"acme/widgets:8:{HEAD}:shadow")
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET state='launching', owner='w', launch_intent=1 WHERE id=?",
                        (row["id"],))
            if created is not None:
                con.execute("UPDATE runs SET created=? WHERE id=?", (created, row["id"]))
        self.seen = {}

        def turn(loop, scope, **kwargs):
            self.seen.update(role=scope.role, no_write=kwargs.get("no_write"),
                             prompt=kwargs.get("prompt"), steps=kwargs.get("steps"),
                             turn_key=scope.turn_key, shadow=scope.shadow)
            kwargs["observed"]["submissions"] = list(submissions)
            return rc

        def prompt(loop, row, reviews, marker=None, change=None):
            self.seen.update(prompt_seat=row["seat"], reviews=list(reviews))
            return "PROMPT"
        pr = pr or {"number": 8, "state": "open", "draft": False,
                    "head": {"sha": HEAD, "ref": "fix-8"}, "user": {"login": "fixer"}}
        self.ci_reads = []

        def read(loop, head):
            self.ci_reads.append(head)
            return checks
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
                mock.patch.object(ci, "read", side_effect=read), \
                mock.patch.object(seat_model, "load_runtime", return_value={
                    "source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}), \
                mock.patch.object(seat_model, "resolve_seat", return_value=self.inference) as resolved, \
                mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=list(reviews)), \
                mock.patch.object(turn_prompts, "isolated_prompt", side_effect=prompt), \
                mock.patch.object(pr_facts, "pr_change", return_value=None), \
                mock.patch.object(trusted_turn, "run_turn", side_effect=turn), \
                mock.patch.object(claims, "claim_seat",
                                  side_effect=AssertionError("a shadow claims no seat")), \
                mock.patch.object(Supervisor, "failure_notice",
                                  side_effect=AssertionError("a shadow pages nobody")), \
                mock.patch.object(sup, "recover"):
            sup._run_production(row["id"], "w")
        self.resolved_seat = resolved.call_args.args[1]
        return sup, sup.get(f"acme/widgets:8:{HEAD}:shadow")

    def test_the_shadow_runs_the_reviewers_prompt_no_write_on_its_own_model(self):
        before = {"id": 1, "state": "CHANGES_REQUESTED", "submitted_at": "2020-01-01T00:00:00Z",
                  "commit_id": "b" * 40, "user": {"login": "reviewer"}, "body": "old round"}
        after = {"id": 2, "state": "APPROVED", "submitted_at": "2099-01-01T00:00:00Z",
                 "commit_id": HEAD, "user": {"login": "reviewer"}, "body": "the real verdict"}
        sup, row = self.run_shadow([{"verdict": "REQUEST_CHANGES", "body": "F1: a.py:1: x",
                                     "authorized": True, "denial": ""}], reviews=[before, after])
        self.assertEqual(row["state"], "succeeded", row)
        self.assertEqual(self.resolved_seat, "shadow")          # its own profile, never Vex's
        self.assertEqual((self.seen["role"], self.seen["no_write"]), ("reviewer", True))
        self.assertTrue(self.seen["shadow"])        # its broker refuses it with no snapshot (#1139)
        self.assertEqual(self.seen["prompt_seat"], "reviewer")
        # The real review posted after the shadow was queued is not shown to it.
        self.assertEqual([r["id"] for r in self.seen["reviews"]], [1])
        got = sup.shadow_reviews("acme/widgets", 8)
        self.assertEqual([(r["verdict"], r["model"], r["body"]) for r in got],
                         [("REQUEST_CHANGES", "grok-x", "F1: a.py:1: x")])

    def test_the_broker_scope_names_the_shadows_own_turn_key(self):
        # #1139: a shadow is judged on the findings snapshot kept for its own reviewer turn, so
        # the scope the broker is built from carries the row's turn key (it reads the snapshot
        # by PR, head and turn). Without it the broker would read the key of no turn at all.
        sup = self.queue_shadow()
        self.reviewer_is(sup, ("confirmed", "APPROVED"), "succeeded")
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET turn_key='retarget' WHERE seat IN ('reviewer','shadow')")
        sup, row = self.claim_shadow(sup, [{"verdict": "APPROVE", "body": "ok",
                                            "authorized": True, "denial": ""}])
        self.assertEqual(row["state"], "succeeded", row)
        self.assertEqual(self.seen["turn_key"], "retarget")

    def test_the_shadow_waits_for_ci_like_the_reviewer_and_spends_nothing(self):
        # A shadow review is compared with the reviewer's, who never reviews a head CI is still
        # deciding: running, cancelled, missing or unreadable checks hold it, nothing launched.
        for name, checks in (("running", ci.CIState(pending=["tests"])),
                             ("cancelled", ci.CIState(cancelled=["tests"])),
                             ("unreadable", None)):
            with self.subTest(name):
                self.setUp()
                sup, row = self.run_shadow([{"verdict": "APPROVE", "body": "ok"}], checks=checks)
                self.assertEqual(self.seen, {}, "no shadow turn launched")
                self.assertEqual(row["state"], "waiting")
                self.assertTrue(row["error"].startswith(run_supervisor.CI_HOLD), row["error"])
                self.assertEqual(row["retries"], 0)
                self.assertEqual(sup.shadow_reviews(), [])

    def test_a_required_check_that_never_reported_holds_the_shadow(self):
        # Review on #1002: the loop names "tests"; only "lint" reported, so "tests" is missing.
        self.loop = {**self.loop, "required_checks": ["tests"]}
        sup, row = self.run_shadow([{"verdict": "APPROVE", "body": "ok"}],
                                   checks=ci.CIState(passed=["lint"]))
        self.assertEqual(self.seen, {}, "no shadow turn launched")
        self.assertEqual((row["state"], row["retries"]), ("waiting", 0))
        self.assertTrue(row["error"].startswith(run_supervisor.CI_HOLD), row["error"])
        self.assertIn("never reported", row["error"])
        self.assertEqual(sup.shadow_reviews(), [])

    def test_the_shadow_never_reviews_a_red_head(self):
        sup, row = self.run_shadow([{"verdict": "APPROVE", "body": "ok"}],
                                   checks=ci.CIState(failed=["tests"], passed=["lint"]))
        self.assertEqual(self.seen, {})
        self.assertEqual(row["state"], "cancelled")
        self.assertIn("CI failed", row["error"])
        self.assertEqual(sup.shadow_reviews(), [])

    def test_a_conflicted_head_is_held_and_unfinished_ci_past_the_wait_is_given_up(self):
        dirty = {"number": 8, "state": "open", "draft": False, "mergeable_state": "dirty",
                 "head": {"sha": HEAD, "ref": "fix-8"}, "user": {"login": "fixer"}}
        _, row = self.run_shadow([], pr=dirty)
        self.assertEqual((row["state"], self.seen), ("waiting", {}))
        self.assertIn("conflicts with its base", row["error"])
        self.setUp()
        # Past the CI wait since the approval (review of #1656, F11: its clock starts there).
        old = time.time() - run_supervisor.CI_WAIT_MAX_S - 1
        sup = self.queue_shadow()
        self.reviewer_is(sup, ("confirmed", "APPROVED"), "succeeded", confirmed=old)
        _, row = self.claim_shadow(sup, [], checks=ci.CIState(pending=["tests"]), created=old)
        self.assertEqual((row["state"], self.seen), ("cancelled", {}))   # never a late review
        self.assertIn("CI did not finish", row["error"])

    def test_a_claimed_shadow_whose_pr_went_draft_or_closed_is_not_sampled(self):
        # #1003: the conflict hold applies to an open, non-draft PR only, and a row claimed
        # before the PR went draft/closed must not fall through to a sample. A draft waits
        # (even past the CI wait, with green checks); a closed PR retires the row.
        old = time.time() - run_supervisor.CI_WAIT_MAX_S - 1
        for name, extra, state in (("draft", {"draft": True}, "waiting"),
                                   ("closed", {"state": "closed"}, "cancelled")):
            for mergeable in ("dirty", "clean"):
                with self.subTest(name, mergeable=mergeable):
                    self.setUp()
                    pr = {"number": 8, "state": "open", "draft": False,
                          "mergeable_state": mergeable,
                          "head": {"sha": HEAD, "ref": "fix-8"}, "user": {"login": "fixer"},
                          **extra}
                    sup, row = self.run_shadow([{"verdict": "APPROVE", "body": "ok",
                                                 "authorized": True, "denial": ""}],
                                               pr=pr, created=old)
                    self.assertEqual((row["state"], self.seen), (state, {}), row)
                    self.assertEqual(sup.shadow_reviews(), [])
                    self.assertNotIn("conflicts with its base", row["error"] or "")
                    self.assertNotIn("CI did not finish", row["error"] or "")
                    self.assertIn(name, row["error"])

    def test_green_ci_reviews_and_reads_the_head_it_reviews(self):
        _, row = self.run_shadow([{"verdict": "APPROVE", "body": "ok", "authorized": True,
                                   "denial": ""}])
        self.assertEqual(row["state"], "succeeded", row)
        self.assertEqual(self.ci_reads, [HEAD])

    def test_a_shadow_that_submits_nothing_fails_once_and_is_not_retried(self):
        # #218: grok-4.7's first live shadow ran out of steps, exited 1, and was retried three
        # times, four turns' tokens for nothing. A shadow is a sample: one attempt.
        sup, row = self.run_shadow([], rc=1)
        self.assertEqual(sup.shadow_reviews(), [])
        self.assertEqual((row["state"], row["retries"]), ("failed", 0))

    def test_a_head_the_reviewer_sent_back_gets_no_shadow_and_spends_nothing(self):
        # #1647: on a change-requested head the shadow decides nothing, so it never runs.
        sup, row = self.run_shadow([{"verdict": "APPROVE", "body": "ok"}],
                                   reviewer=("confirmed", "CHANGES_REQUESTED"))
        self.assertEqual(self.seen, {}, "no shadow turn launched")
        self.assertEqual(row["state"], "cancelled")
        self.assertIn("the reviewer requested changes", row["error"])
        self.assertEqual(sup.shadow_reviews(), [])

    def test_the_shadow_waits_for_its_reviewers_verdict_and_spends_nothing(self):
        # #1647: the reviewer's review is still being posted (receipt claimed, not confirmed),
        # or no receipt yet: the shadow is held and polled again, nothing launched.
        for name, reviewer, state in (("posting", ("claimed", "APPROVE"), "running"),
                                      ("running", None, "running"),
                                      ("uncertain", None, "uncertain")):
            with self.subTest(name):
                self.setUp()
                sup, row = self.run_shadow([{"verdict": "APPROVE", "body": "ok"}],
                                           reviewer=reviewer, reviewer_state=state)
                self.assertEqual(self.seen, {}, "no shadow turn launched")
                self.assertEqual(row["state"], "waiting")
                self.assertIn("waiting for the reviewer's verdict", row["error"])
                self.assertEqual(row["retries"], 0)

    def test_a_receipt_that_can_never_confirm_ends_the_shadow_instead_of_waiting(self):
        # #1755 / #1771: a `posted` receipt (the PR went ineligible under the posted review) or
        # a `claimed` one on a reconciled (failed) reviewer is terminal. The claimed shadow row
        # is cancelled, not re-armed every poll for ever, and nothing is launched.
        for name, receipt, state in (("posted", "posted", "succeeded"),
                                     ("reconciled", "claimed", "failed")):
            with self.subTest(name):
                self.setUp()
                sup, row = self.run_shadow([{"verdict": "APPROVE", "body": "ok"}],
                                           reviewer=(receipt, "APPROVE"), reviewer_state=state)
                self.assertEqual((self.seen, row["state"]), ({}, "cancelled"))
                self.assertIn("never confirmed", row["error"])

    def test_a_reviewer_turn_that_ended_without_a_verdict_gets_no_shadow(self):
        sup, row = self.run_shadow([{"verdict": "APPROVE", "body": "ok"}], reviewer=None)
        self.assertEqual((self.seen, row["state"]), ({}, "cancelled"))
        self.assertIn("ended succeeded without a verdict", row["error"])

    def test_a_shadow_waits_however_long_its_reviewer_takes(self):
        # Review of #1656, F9: no time bound. One would end the shadow with the head clear, and
        # a late APPROVE would then be green with no shadow review.
        sup, row = self.run_shadow([{"verdict": "APPROVE", "body": "ok"}],
                                   reviewer=("claimed", "APPROVE"), reviewer_state="running",
                                   created=time.time() - 30 * 86400)
        self.assertEqual((self.seen, row["state"]), ({}, "waiting"))
        self.assertTrue(row["error"].startswith(f"{run_supervisor.VERDICT_HOLD} at {HEAD[:7]} — "),
                        row["error"])

    def test_a_verdict_held_shadow_ends_when_the_pr_closes_or_the_head_moves(self):
        # #1776: the hold returns before any GitHub read, so the end the docs promise is the
        # claim's: the held row falls due, recover() re-arms it, and _claim_read_shadow (never
        # _ci_poll's reviewer-only fast path) cancels it before _shadow_ci_gate runs again.
        closed = {"number": 8, "state": "closed", "draft": False, "head": {"sha": HEAD}}
        moved = {"number": 8, "state": "open", "draft": False, "head": {"sha": "b" * 40}}
        for name, state, pr in (("failed, closed", "failed", closed),
                                ("failed, moved", "failed", moved),
                                ("uncertain, closed", "uncertain", closed),
                                ("uncertain, moved", "uncertain", moved)):
            with self.subTest(name):
                self.setUp()
                sup, row = self.run_shadow([{"verdict": "APPROVE", "body": "ok"}],
                                           reviewer=None, reviewer_state=state)
                self.assertEqual(row["state"], "waiting")
                self.assertIn("waiting for the reviewer's verdict", row["error"])
                with closing(sqlite3.connect(sup.db)) as con, con:
                    con.execute("UPDATE runs SET retry_at=? WHERE id=?",
                                (time.time() - 1, row["id"]))
                sup.recover()
                key = f"acme/widgets:8:{HEAD}:shadow"
                self.assertEqual(sup.get(key)["state"], "pending")
                with mock.patch.object(config, "by_repo", return_value=self.loop), \
                        mock.patch.object(gh, "api", return_value=pr), \
                        mock.patch.object(claims, "claim_seat",
                                          side_effect=AssertionError("a shadow claims no seat")), \
                        mock.patch.object(sup, "_spawn"):
                    sup._claim()
                row = sup.get(key)
                self.assertEqual(row["state"], "cancelled", row["error"])
                self.assertEqual(row["error"], "shadow review superseded: PR closed or head moved")
                # No longer open, so its findings snapshot is evictable again.
                self.assertNotIn((8, HEAD), gate.shadow_open_heads("acme/widgets", sup.db))

    def kinds(self):
        pr = {"number": 8, "draft": False, "user": {"login": "fixer"}, "base": {"ref": "main"},
              "head": {"sha": HEAD, "ref": "fix-8"}}
        approval = {"id": 1, "state": "APPROVED", "commit_id": HEAD,
                    "submitted_at": "2026-01-01T00:00:00Z", "user": {"login": "reviewer", "id": 7}}
        detail = {"mergeable": True, "mergeable_state": "clean", "number": 8, "state": "open",
                  "draft": False, "head": {"sha": HEAD}, "base": {"ref": "main"}}
        with mock.patch.object(ready.gate_failures, "open_for", return_value=[]):
            return ready.assess_kinds(self.loop, state_mod.state_for(self.loop), pr, [approval],
                                      GREEN, detail)

    def test_a_shadow_claimed_before_its_reviewer_approves_holds_the_head_until_it_lands(self):
        # Review of #1656, F7: the whole transition, through the real enqueue, the real claim,
        # ``ready`` and the merge check. Launched beside its reviewer, as before #1647, the
        # shadow would run at the first claim, while the reviewer's APPROVE is still posting.
        self.loop = config.normalize({**RAW, "state_dir": str(self.home / "state"),
                                      "required_checks": ["tests"],
                                      "shadow": {"profile": "grok", "every": 1, "escalate": True}})
        approve = [{"verdict": "APPROVE", "body": "ok", "authorized": True}]
        sup = self.queue_shadow()
        self.reviewer_is(sup, ("claimed", "APPROVE"), "running")    # GitHub already shows it
        sup, row = self.claim_shadow(sup, approve)
        self.assertEqual((self.seen, row["state"]), ({}, "waiting"), "no shadow launched yet")
        self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.PENDING)
        self.reviewer_is(sup, ("confirmed", "APPROVED"), "succeeded")
        self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.PENDING)
        self.assertTrue([w for _k, w in self.kinds() if w.startswith("shadow review")])
        sup, row = self.claim_shadow(sup, approve)
        self.assertEqual((self.seen["role"], row["state"]), ("reviewer", "succeeded"), row)
        self.assertEqual([w for _k, w in self.kinds() if w.startswith("shadow review")], [])

    def test_escalate_with_always_paths_holds_their_heads_and_not_the_others(self):
        # #1788 (review of #1852, F3): escalate with every > 1 is allowed only beside always
        # paths. A head sampling alone skips (every 6 skips HEAD) is shadowed, held while the
        # shadow is pending and after it dissents when the PR changes an always path, and is
        # neither shadowed nor held when it doesn't.
        from diaktoros import shadow_choice
        shadow = {"profile": "grok", "every": 6, "escalate": True, "always_paths": ["src/auth/*"]}
        self.loop = config.normalize({**RAW, "state_dir": str(self.home / "state"),
                                      "required_checks": ["tests"], "shadow": shadow})
        self.assertFalse(shadow_choice.sampled(self.loop, 8, HEAD))
        with mock.patch.object(gh, "pr_files_read",
                               return_value=([{"filename": "src/auth/login.py"}], "")):
            sup = self.queue_shadow()
        self.reviewer_is(sup, ("confirmed", "APPROVED"), "succeeded")
        self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.PENDING)
        self.assertTrue([w for _k, w in self.kinds() if w.startswith("shadow review")])
        sup, row = self.claim_shadow(sup, [{"verdict": "REQUEST_CHANGES", "body": "F1: a.py:1: x",
                                            "authorized": True, "denial": ""}])
        self.assertEqual(row["state"], "succeeded", row["error"])
        held = [w for _k, w in self.kinds() if w.startswith("shadow review")]
        self.assertTrue(held and "requested changes" in held[0], held)
        self.assertNotEqual(merge_check.decide(self.kinds())[0], merge_check.SUCCESS)
        # The same loop, a PR that changes no always path: sampled out, no shadow, no hold.
        self.setUp()
        self.loop = config.normalize({**RAW, "state_dir": str(self.home / "state"),
                                      "required_checks": ["tests"], "shadow": shadow})
        with mock.patch.object(Supervisor, "_spawn"), \
                mock.patch.object(config, "by_repo", return_value=self.loop), \
                mock.patch.object(gh, "pr_files_read",
                                  return_value=([{"filename": "lib/util.py"}], "")):
            gate.enqueue_isolated(self.loop, "reviewer", 8, HEAD)
        sup = Supervisor(self.home / "state" / "diaktoros-runs.sqlite",
                         production_config=self.home / "diaktoros-runtime.json",
                         hermes_home=self.home)
        self.assertIsNone(sup.get(f"acme/widgets:8:{HEAD}:shadow"))
        self.reviewer_is(sup, ("confirmed", "APPROVED"), "succeeded")
        self.assertEqual([w for _k, w in self.kinds() if w.startswith("shadow review")], [])

    def test_a_reviewer_that_failed_without_a_verdict_keeps_its_shadow_for_its_retry(self):
        # Review of #1656, F14: the reviewer turn fails with no verdict and is re-armed, by a
        # redelivered event or by an operator. Cancelled at that first claim, the shadow would be
        # gone when the retried turn approves, and the head would read green with no second
        # opinion. Held, it runs on the approval, and the head holds until it lands.
        approve = [{"verdict": "APPROVE", "body": "ok", "authorized": True}]
        for how in ("redelivery", "operator retry"):
            with self.subTest(how):
                self.setUp()
                self.loop = config.normalize({**RAW, "state_dir": str(self.home / "state"),
                                              "required_checks": ["tests"],
                                              "shadow": {"profile": "grok", "every": 1,
                                                         "escalate": True}})
                sup = self.queue_shadow()
                self.reviewer_is(sup, None, "failed")
                sup, row = self.claim_shadow(sup, approve)
                self.assertEqual((self.seen, row["state"]), ({}, "waiting"), row["error"])
                reviewer = sup.get(f"acme/widgets:8:{HEAD}:reviewer")
                if how == "redelivery":
                    with mock.patch.object(Supervisor, "_spawn"), \
                            mock.patch.object(config, "by_repo", return_value=self.loop):
                        self.assertEqual(gate.enqueue_isolated(self.loop, "reviewer", 8, HEAD),
                                         "rearmed")
                else:
                    with mock.patch.object(Supervisor, "_spawn"):
                        sup.retry(reviewer["id"])
                self.assertEqual(sup.get(f"acme/widgets:8:{HEAD}:reviewer")["state"], "pending")
                self.reviewer_is(sup, ("confirmed", "APPROVED"), "succeeded")
                self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.PENDING)
                self.assertTrue([w for _k, w in self.kinds() if w.startswith("shadow review")])
                sup, row = self.claim_shadow(sup, approve)
                self.assertEqual((self.seen.get("role"), row["state"]), ("reviewer", "succeeded"),
                                 row["error"])
                self.assertEqual([w for _k, w in self.kinds() if w.startswith("shadow review")],
                                 [])

    def test_a_reviewer_superseded_at_its_head_ends_its_shadow_and_the_head_clears(self):
        # Review of #1656, F15: a reviewer turn cancelled because a review already answers its
        # head is never re-armed (`retry` refuses it; a redelivery re-arms it only to be
        # cancelled again). A shadow waiting for it would hold the approved head for ever.
        self.loop = config.normalize({**RAW, "state_dir": str(self.home / "state"),
                                      "required_checks": ["tests"],
                                      "shadow": {"profile": "grok", "every": 1, "escalate": True}})
        sup = self.queue_shadow()
        self.reviewer_is(sup, None, "cancelled")
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET error='superseded: reviewed at this head by reviewer' "
                        "WHERE seat='reviewer'")
        sup, row = self.claim_shadow(sup, [{"verdict": "APPROVE", "body": "ok",
                                            "authorized": True}])
        self.assertEqual((self.seen, row["state"]), ({}, "cancelled"), row["error"])
        self.assertIn("without a verdict", row["error"])
        self.assertEqual([w for _k, w in self.kinds() if w.startswith("shadow review")], [])

    def test_a_late_approval_starts_the_ci_wait_so_a_transient_ci_read_holds_the_head(self):
        # Review of #1656, F11: the shadow waited for its reviewer far past the CI wait. Timed
        # from the queueing, the first unreadable CI answer would cancel it and the approved head
        # would read green with no second opinion. Timed from the approval, it holds and polls.
        self.loop = config.normalize({**RAW, "state_dir": str(self.home / "state"),
                                      "required_checks": ["tests"],
                                      "shadow": {"profile": "grok", "every": 1, "escalate": True}})
        approve = [{"verdict": "APPROVE", "body": "ok", "authorized": True}]
        sup = self.queue_shadow()
        self.reviewer_is(sup, ("confirmed", "APPROVED"), "succeeded")      # confirmed just now
        long_ago = time.time() - 2 * run_supervisor.CI_WAIT_MAX_S
        sup, row = self.claim_shadow(sup, approve, checks=None, created=long_ago)
        self.assertEqual((self.seen, row["state"]), ({}, "waiting"), row["error"])
        self.assertTrue(row["error"].startswith(run_supervisor.CI_HOLD), row["error"])
        self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.PENDING)
        self.assertTrue([w for _k, w in self.kinds() if w.startswith("shadow review")])
        sup, row = self.claim_shadow(sup, approve)                          # CI reads green now
        self.assertEqual((self.seen["role"], row["state"]), ("reviewer", "succeeded"), row)
        self.assertEqual([w for _k, w in self.kinds() if w.startswith("shadow review")], [])

    def test_the_ci_clock_is_read_with_the_verdict_so_a_later_failed_read_cannot_reset_it(self):
        # Review of #1656, F11 (round 5): the approval's time used to be a second ledger read
        # after the verdict's. When only that read failed, the clock fell back to the queueing,
        # and one unreadable CI answer then cancelled the shadow with the head clear. Here every
        # ledger read after the first fails; the shadow holds, and recovers once CI reads green.
        self.loop = config.normalize({**RAW, "state_dir": str(self.home / "state"),
                                      "required_checks": ["tests"],
                                      "shadow": {"profile": "grok", "every": 1, "escalate": True}})
        approve = [{"verdict": "APPROVE", "body": "ok", "authorized": True}]
        sup = self.queue_shadow()
        self.reviewer_is(sup, ("confirmed", "APPROVED"), "succeeded")      # confirmed just now
        real, reads = views.read_only_connect, []

        def first_read_only(db):
            reads.append(db)
            return real(db) if len(reads) == 1 else None
        long_ago = time.time() - 2 * run_supervisor.CI_WAIT_MAX_S
        with mock.patch.object(views, "read_only_connect", side_effect=first_read_only):
            sup, row = self.claim_shadow(sup, approve, checks=None, created=long_ago)
        self.assertEqual((self.seen, row["state"]), ({}, "waiting"), row["error"])
        self.assertTrue(row["error"].startswith(run_supervisor.CI_HOLD), row["error"])
        self.assertEqual(merge_check.decide(self.kinds())[0], merge_check.PENDING)
        sup, row = self.claim_shadow(sup, approve)                          # CI reads green now
        self.assertEqual((self.seen["role"], row["state"]), ("reviewer", "succeeded"), row)
        self.assertEqual([w for _k, w in self.kinds() if w.startswith("shadow review")], [])

    def test_the_shadow_takes_its_own_step_budget(self):
        self.run_shadow([{"verdict": "APPROVE", "body": "ok", "authorized": True}])
        self.assertEqual(self.seen["steps"], config.DEFAULT_SHADOW_MAX_STEPS)
        self.loop = config.normalize({**RAW, "state_dir": str(self.home / "state2"),
                                      "shadow": {"profile": "grok", "every": 1},
                                      "seats": {**RAW["seats"], "shadow": {"max_steps": 150}}})
        self.run_shadow([{"verdict": "APPROVE", "body": "ok", "authorized": True}])
        self.assertEqual(self.seen["steps"], 150)


LOOP_ID = "shadows"


class Cli(unittest.TestCase):
    """Every path that sets the two shadow settings (#323): form, init, setup, set, apply."""

    @classmethod
    def setUpClass(cls):
        import run_tests as t
        cls.t = t
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t = self.t
        t.reset(prs={})
        (t.LOOPS_DIR / "widgets.json").unlink(missing_ok=True)
        self.file = t.LOOPS_DIR / f"{LOOP_ID}.json"
        self.file.unlink(missing_ok=True)
        self.addCleanup(self.file.unlink, missing_ok=True)

    def cli(self, *argv, settings=None):
        return self.t.run_cli(self.t.parser_for(settings).parse_args(list(argv)))

    def init(self, *extra, settings=None):
        t = self.t
        return self.cli("init", "--repo", t.REPO, "--id", LOOP_ID, "--host", t.HOST,
                        "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                        "--reviewer-profile", "reviewer-profile", "--fixer-profile", "fixer-profile",
                        "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                        "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *t.READER_ARGS, *extra,
                        settings=settings)

    def shadow(self) -> dict:
        return json.loads(self.file.read_text()).get("shadow", {})

    def test_init_from_the_form_and_the_flags_win(self):
        form = {"shadow_profile": "grok", "shadow_every": "3"}
        for extra, settings, want in (
                ((), None, {}),
                ((), form, {"profile": "grok", "every": 3}),
                (("--shadow-profile", "other", "--shadow-every", "2"), form,
                 {"profile": "other", "every": 2})):
            with self.subTest(extra=extra, settings=settings):
                self.file.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.shadow(), want)

    def test_init_takes_shadow_escalate_from_the_form_and_the_flag(self):
        # #1004 (shadow review of #1097, F3): init's own path for the setting, form and flag.
        form = {"shadow_profile": "grok", "shadow_every": "1", "shadow_escalate": "on"}
        for extra, settings, want in (
                ((), form, {"profile": "grok", "every": 1, "escalate": True}),
                (("--shadow-escalate", "off"), form, {"profile": "grok", "every": 1}),
                (("--shadow-profile", "x", "--shadow-every", "1", "--shadow-escalate", "on"),
                 None, {"profile": "x", "every": 1, "escalate": True})):
            with self.subTest(extra=extra, settings=settings):
                self.file.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.shadow(), want)
        self.file.unlink(missing_ok=True)
        rc, out = self.init("--shadow-profile", "x", "--shadow-escalate", "on")
        self.assertNotEqual(rc, 0)                         # every defaults to 5: refused
        self.assertIn("every 1", out)

    def test_setup_hands_init_shadow_escalate(self):
        from diaktoros.cli import setup_command as cli_setup
        t = self.t
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--shadow-profile", "x",
                                            "--shadow-every", "1", "--shadow-escalate", "on"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--shadow-escalate=on", argv)

    def test_set_turns_it_on_rates_it_off_and_refuses_and_apply_moves_it(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-every", "2")
        self.assertEqual(rc, 2, out)                      # a rate needs a profile
        self.assertIn("needs a shadow profile", out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-profile", "grok")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.shadow(), {"profile": "grok"})
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-every", "3")
        self.assertEqual((rc, self.shadow()), (0, {"profile": "grok", "every": 3}), out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-every", "0")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.shadow(), {"profile": "grok", "every": 3})
        rc, out = self.cli("set", "--loop", LOOP_ID, "--no-shadow")
        self.assertEqual((rc, self.shadow()), (0, {}), out)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"shadow_profile": "grok",
                                                                "shadow_every": "4"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.shadow(), {"profile": "grok", "every": 4})
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"shadow_profile": "off"})
        self.assertEqual((rc, self.shadow()), (0, {}), out)

    def test_shadow_issues_daily_in_every_path(self):
        # #1515: init (flag and form), set, apply, setup and show, with the 0-1000 bound.
        form = {"shadow_profile": "grok", "shadow_issues_daily": "40"}
        for extra, settings, want in (
                ((), form, {"profile": "grok", "issues_daily": 40}),
                (("--shadow-issues-daily", "0"), form, {"profile": "grok", "issues_daily": 0}),
                (("--shadow-profile", "x", "--shadow-issues-daily", "7"), None,
                 {"profile": "x", "issues_daily": 7})):
            with self.subTest(extra=extra, settings=settings):
                self.file.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.shadow(), want)
        self.file.unlink(missing_ok=True)
        rc, out = self.init("--shadow-profile", "x", "--shadow-issues-daily", "1001")
        self.assertNotEqual(rc, 0)
        self.assertIn("issues_daily", out)
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-issues-daily", "5")
        self.assertEqual(rc, 2, out)                      # needs a profile
        self.assertIn("needs a shadow profile", out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-profile", "grok",
                           "--shadow-issues-daily", "50")
        self.assertEqual((rc, self.shadow()), (0, {"profile": "grok", "issues_daily": 50}), out)
        rc, out = self.cli("show", "--loop", LOOP_ID)
        self.assertIn("50", next(line for line in out.splitlines()
                                 if line.strip().startswith("shadow_issues_daily")))
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-issues-daily", "1001")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.shadow(), {"profile": "grok", "issues_daily": 50})
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"shadow_issues_daily": "0"})
        self.assertEqual((rc, self.shadow()), (0, {"profile": "grok", "issues_daily": 0}), out)
        from diaktoros.cli import setup_command as cli_setup
        t = self.t
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--shadow-profile", "x",
                                            "--shadow-issues-daily", "9"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--shadow-issues-daily=9", argv)

    def test_shadow_path_globs_in_every_path(self):
        # #1788: init (flag and form), set (replace and clear), apply, setup and show.
        form = {"shadow_profile": "grok", "shadow_always_paths": "src/auth/*, deploy/*",
                "shadow_never_paths": "docs/*"}
        for extra, settings, want in (
                ((), form, {"profile": "grok", "always_paths": ["src/auth/*", "deploy/*"],
                            "never_paths": ["docs/*"]}),
                (("--shadow-always-path", "lib/*"), form,
                 {"profile": "grok", "always_paths": ["lib/*"], "never_paths": ["docs/*"]}),
                (("--shadow-profile", "x", "--shadow-never-path", "*.md",
                  "--shadow-never-path", "tests/*"), None,
                 {"profile": "x", "never_paths": ["*.md", "tests/*"]})):
            with self.subTest(extra=extra, settings=settings):
                self.file.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.shadow(), want)
        self.file.unlink(missing_ok=True)
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-always-path", "a/*")
        self.assertEqual(rc, 2, out)                      # needs a profile
        self.assertIn("need a shadow profile", out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-profile", "grok",
                           "--shadow-always-path", "a/*", "--shadow-always-path", "b/*",
                           "--shadow-never-path", "docs/*")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.shadow(), {"profile": "grok", "always_paths": ["a/*", "b/*"],
                                         "never_paths": ["docs/*"]})
        rc, out = self.cli("show", "--loop", LOOP_ID)
        self.assertIn("b/*", next(line for line in out.splitlines()
                                  if line.strip().startswith("shadow_always_paths")))
        rc, out = self.cli("set", "--loop", LOOP_ID, "--no-shadow-always-paths")
        self.assertEqual((rc, self.shadow()), (0, {"profile": "grok",
                                                   "never_paths": ["docs/*"]}), out)
        rc, out = self.cli("apply", "--loop", LOOP_ID,
                           settings={"shadow_always_paths": "c/*, d/*"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.shadow()["always_paths"], ["c/*", "d/*"])
        from diaktoros.cli import setup_command as cli_setup
        t = self.t
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--shadow-profile", "x",
                                            "--shadow-never-path", "docs/*"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--shadow-never-path=docs/*", argv)

    def seat(self) -> dict:
        return json.loads(self.file.read_text()).get("seats", {}).get("shadow", {})

    def test_the_shadow_seats_limits_in_every_path(self):
        # #875: they were loop-file only, and show never listed daily_tokens.
        rc, out = self.init("--shadow-profile", "grok", "--shadow-daily-tokens", "500000")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.seat(), {"daily_tokens": 500000})
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-daily-tokens", "2000000",
                           "--shadow-daily-turns", "6", "--shadow-turn-budget", "1200",
                           "--shadow-max-steps", "120")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.seat(), {"daily_tokens": 2000000, "daily_turns": 6,
                                       "turn_budget_s": 1200, "max_steps": 120})
        # Each change is named for what it is (the printer said "turn budget …s" for tokens).
        self.assertIn("shadow daily tokens: 500000 → 2000000", out)
        self.assertIn("shadow daily turns: no cap → 6", out)
        self.assertIn(f"shadow turn budget: {config.DEFAULT_TURN_BUDGET_S}s → 1200s", out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--reviewer-daily-tokens", "300000")
        self.assertIn("reviewer daily tokens: no budget → 300000", out)
        self.assertNotIn("tokens turn budget", out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-max-steps", "500")
        self.assertEqual(rc, 2, out)                            # out of range: refused
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-daily-turns", "0",
                           "--shadow-turn-budget", "0", "--shadow-max-steps", "0")
        self.assertEqual((rc, self.seat()), (0, {"daily_tokens": 2000000}), out)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"shadow_daily_tokens": "750000"})
        self.assertEqual((rc, self.seat()), (0, {"daily_tokens": 750000}), out)
        rc, out = self.cli("show", "--loop", LOOP_ID)
        self.assertIn("750000", next(line for line in out.splitlines()
                                     if line.strip().startswith("shadow_daily_tokens")))
        from diaktoros.cli import setup_command as cli_setup
        t = self.t
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO,
                                            "--shadow-daily-tokens", "9"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--shadow-daily-tokens=9", argv)

    def test_dropping_the_shadow_budget_shows_the_loop_default_set_alongside(self):
        # #907: `--turn-budget N --shadow-turn-budget 0` printed the old loop default.
        rc, out = self.init("--shadow-profile", "grok")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--shadow-turn-budget", "1200")
        self.assertEqual(rc, 0, out)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--turn-budget", "600",
                           "--shadow-turn-budget", "0")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.seat(), {})
        self.assertEqual(json.loads(self.file.read_text())["turn_budget_s"], 600)
        self.assertIn("shadow turn budget: 1200s → 600s", out)
        self.assertNotIn(f"→ {config.DEFAULT_TURN_BUDGET_S}s", out)

    def test_a_blank_form_moves_nothing(self):
        from diaktoros.config import settings_form
        raw = {**RAW, "shadow": {"profile": "grok"}}
        self.assertEqual(settings_form.apply_settings(dict(raw), {})["shadow"], {"profile": "grok"})
        self.assertIn("shadow_profile", settings_form.SETTINGS_SCHEMA)
        self.assertIn("shadow_every", settings_form.SETTINGS_SCHEMA)

    def test_setup_hands_init_the_profile(self):
        from diaktoros.cli import setup_command as cli_setup
        t = self.t
        args = t.parser_for({"shadow_profile": "grok"}).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--shadow")], ["--shadow-profile=grok"])
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--shadow-profile", "x",
                                            "--shadow-every", "2"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--shadow")],
                         ["--shadow-profile=x", "--shadow-every=2"])


if __name__ == "__main__":
    unittest.main()
