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
from diaktoros import config, gate, gh, run_supervisor, seat_model, stats, trusted_turn  # noqa: E402
from diaktoros.run_supervisor import Supervisor, pr_facts, turn_prompts  # noqa: E402

HEAD = "a" * 40
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
        from diaktoros import doctor
        loop = config.normalize({**RAW, "shadow": {"profile": shadow}})
        with mock.patch.object(doctor, "runtime_settings", return_value=({}, None)), \
                mock.patch.object(seat_model, "describe_seat",
                                  return_value=("ok", "described", "")):
            return {c.name: c for c in doctor.check_seat_models(loop)}

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

    def run_shadow(self, submissions, reviews=()):
        with mock.patch.object(Supervisor, "_spawn"), \
                mock.patch.object(config, "by_repo", return_value=self.loop):
            gate.enqueue_isolated(self.loop, "reviewer", 8, HEAD)
        sup = Supervisor(self.home / "state" / "diaktoros-runs.sqlite",
                         production_config=self.home / "diaktoros-runtime.json",
                         hermes_home=self.home)
        row = sup.get(f"acme/widgets:8:{HEAD}:shadow")
        self.assertIsNotNone(row, "the sampled reviewer turn queued a shadow")
        with closing(sqlite3.connect(sup.db)) as con, con:
            con.execute("UPDATE runs SET state='launching', owner='w', launch_intent=1 WHERE id=?",
                        (row["id"],))
        self.seen = {}

        def turn(loop, scope, **kwargs):
            self.seen.update(role=scope.role, no_write=kwargs.get("no_write"),
                             prompt=kwargs.get("prompt"))
            kwargs["observed"]["submissions"] = list(submissions)
            return 0

        def prompt(loop, row, reviews, marker=None, change=None):
            self.seen.update(prompt_seat=row["seat"], reviews=list(reviews))
            return "PROMPT"
        pr = {"number": 8, "state": "open", "draft": False,
              "head": {"sha": HEAD, "ref": "fix-8"}, "user": {"login": "fixer"}}
        with mock.patch.object(config, "by_repo", return_value=self.loop), \
                mock.patch.object(seat_model, "load_runtime", return_value={
                    "source": "/x", "venv": "/x", "runtime": "/x", "rust": "/x"}), \
                mock.patch.object(seat_model, "resolve_seat", return_value=self.inference) as resolved, \
                mock.patch.object(gh, "api", return_value=pr), \
                mock.patch.object(gh, "reviews", return_value=list(reviews)), \
                mock.patch.object(turn_prompts, "isolated_prompt", side_effect=prompt), \
                mock.patch.object(pr_facts, "pr_change", return_value=None), \
                mock.patch.object(trusted_turn, "run_turn", side_effect=turn), \
                mock.patch.object(run_supervisor, "claim_seat",
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
        self.assertEqual(self.seen["prompt_seat"], "reviewer")
        # The real review posted after the shadow was queued is not shown to it.
        self.assertEqual([r["id"] for r in self.seen["reviews"]], [1])
        got = sup.shadow_reviews("acme/widgets", 8)
        self.assertEqual([(r["verdict"], r["model"], r["body"]) for r in got],
                         [("REQUEST_CHANGES", "grok-x", "F1: a.py:1: x")])

    def test_a_shadow_that_submits_nothing_fails_quietly(self):
        sup, row = self.run_shadow([])
        self.assertEqual(sup.shadow_reviews(), [])
        self.assertNotEqual(row["state"], "running")


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

    def test_a_blank_form_moves_nothing(self):
        from diaktoros.config import settings_form
        raw = {**RAW, "shadow": {"profile": "grok"}}
        self.assertEqual(settings_form.apply_settings(dict(raw), {})["shadow"], {"profile": "grok"})
        self.assertIn("shadow_profile", settings_form.SETTINGS_SCHEMA)
        self.assertIn("shadow_every", settings_form.SETTINGS_SCHEMA)

    def test_setup_hands_init_the_profile(self):
        from diaktoros.cli import install as cli_install
        t = self.t
        args = t.parser_for({"shadow_profile": "grok"}).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli_install._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--shadow")], ["--shadow-profile=grok"])
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--shadow-profile", "x",
                                            "--shadow-every", "2"])
        argv, _ = cli_install._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--shadow")],
                         ["--shadow-profile=x", "--shadow-every=2"])


if __name__ == "__main__":
    unittest.main()
