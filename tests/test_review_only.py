"""Review-only authors (#191): the reviewer reviews their PRs, the fixer never touches them.

A changes-requested verdict on a review-only author's PR goes back to that author: no fixer turn,
no verdict cap, no adjudication, and no "fixer never pushed" stall. The author may ask for the
next review themselves. The list moves from every settings path, and a login is never both a
fixer (or a reviewer) and review-only.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import json
import os
import pathlib
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
import test_fixer_gating as fg  # noqa: E402
from review_loop import cli, config, gate, gh, run_supervisor  # noqa: E402
from scripts import gate_fixer, gate_reviewer, watchdog  # noqa: E402

HEAD = fg.HEAD
OWNER = "owner-human"


def raw(**extra) -> dict:
    return {"repo": "acme/widgets", "fixers": ["fix"], "reviewers": ["rev"],
            "read_token": "reader", "tokens": {},
            "seats": {"reviewer": {"profile": "r", "login": "rev", "route": "w-review"},
                      "fixer": {"profile": "f", "login": "fix", "route": "w-fix"}}, **extra}


class Setting(unittest.TestCase):
    def test_default_empty_lowercased_and_validated(self):
        self.assertEqual(config.normalize(raw())["review_only"], [])
        loop = config.normalize(raw(review_only=["Owner-Human"]))
        self.assertEqual(loop["review_only"], [OWNER])
        self.assertEqual(config.review_only(loop), {OWNER})
        self.assertEqual(config.reviewed_authors(loop), {"fix", OWNER})
        for bad in ("owner", ["a", "A"], [""], ["-a"], ["a b"], [3], ["x" * 40],
                    [f"u{i}" for i in range(51)]):
            with self.subTest(value=bad), self.assertRaises(config.ConfigError):
                config.normalize(raw(review_only=bad))

    def test_never_also_a_fixer_or_a_reviewer(self):
        for name in ("fix", "rev", "FIX"):
            with self.subTest(name=name), self.assertRaises(config.ConfigError) as caught:
                config.normalize(raw(review_only=[name]))
            self.assertIn("cannot be review-only", str(caught.exception))

    def test_the_form_moves_it_only_when_it_names_it(self):
        self.assertNotIn("review_only", config.apply_settings(raw(), {}))
        got = config.apply_settings(raw(), {"review_only": " owner-human , other ,"})
        self.assertEqual(got["review_only"], [OWNER, "other"])
        self.assertIn("review_only", (t.ROOT / "plugin.yaml").read_text())


class Gates(fg.Base):
    """Both gates end to end, with GitHub, the worker spawn and the observer mocked."""

    def setUp(self):
        super().setUp()
        self.set_push(True)                       # the fixer could push: it still never runs
        path = config.config_dir() / "one.json"
        data = json.loads(path.read_text())
        path.write_text(json.dumps({**data, "review_only": [OWNER]}))
        self.loop = config.load_id("one")
        self.live = {**fg.LIVE, "user": {"login": OWNER}}

    def verdicts(self, count: int, commit: str = HEAD) -> list:
        return [fg.verdict(rid) | {"commit_id": commit} for rid in range(5, 5 + count)]

    def fixer_gate(self, prior: int = 0):
        rid = 5 + prior
        payload = {"action": "submitted", "repository": {"full_name": fg.REPO},
                   "pull_request": self.live,
                   "review": fg.verdict(rid) | {"state": "changes_requested"}}
        with mock.patch.object(gate_fixer.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             mock.patch.object(gate_fixer.gh, "pr", return_value=self.live), \
             mock.patch.object(gate_fixer.gate, "fetch_reviews",
                               return_value=self.verdicts(prior + 1)), \
             mock.patch.object(gate_fixer.gate, "drain_seat"), \
             mock.patch.object(gate, "enqueue_isolated") as enqueue, \
             mock.patch.object(gate, "breach") as breach, \
             mock.patch.object(gate_fixer.observer, "notify") as notify, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                gate_fixer.main()
        self.assertEqual(out.getvalue().strip(), "[SILENT]")
        return enqueue, breach, notify

    def test_a_verdict_goes_back_to_the_author_not_the_fixer(self):
        for prior in (0, self.loop["cap"] - 1, self.loop["cap"] + 2):   # under, at, past the cap
            with self.subTest(prior=prior):
                enqueue, breach, notify = self.fixer_gate(prior)
                enqueue.assert_not_called()
                breach.assert_not_called()
                self.assertEqual(self.st.queue_items("fixer"), {})
                self.assertEqual(self.ledger_rows(), [])
                self.assertIn("returned to the author", notify.call_args.kwargs["next_turn"])
                self.assertIn(OWNER, notify.call_args.kwargs["next_turn"])

    def test_a_fixer_pr_still_wakes_the_fixer(self):
        self.live = fg.LIVE
        enqueue, _, notify = self.fixer_gate()
        enqueue.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["next_turn"], "fixer queued")

    def reviewer_gate(self, sender: str, rounds: int = 0, author: str = OWNER):
        # Earlier rounds were on an older head: this head is pushed and asks for its review.
        live = {**self.live, "user": {"login": author}}
        payload = {"action": "review_requested", "number": 7, "pull_request": live,
                   "repository": {"full_name": fg.REPO}, "sender": {"login": sender},
                   "requested_reviewer": {"login": "reviewer"}}
        with mock.patch.object(gate_reviewer.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             mock.patch.object(gate_reviewer.gh, "pr", return_value=live), \
             mock.patch.object(gate_reviewer.gate, "fetch_reviews",
                               return_value=self.verdicts(rounds, "c" * 40)), \
             mock.patch.object(gate_reviewer.gate, "drain_seat"), \
             mock.patch.object(gate_reviewer.gate, "breach",
                               side_effect=lambda *a, **kw: gate_reviewer.silence()) as breach, \
             mock.patch.object(gate_reviewer.gate, "block_pr_agent",
                               side_effect=lambda *a, **kw: gate_reviewer.silence()) as block, \
             mock.patch.object(gate_reviewer.observer, "notify"), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                gate_reviewer.main()
        self.assertEqual(out.getvalue().strip(), "[SILENT]")
        return block, breach

    def test_the_author_may_ask_for_their_own_review_and_a_stranger_may_not(self):
        block, _ = self.reviewer_gate(OWNER)
        block.assert_called_once()
        self.assertEqual(block.call_args.args[2:5], ("reviewer", 7, HEAD))
        block, _ = self.reviewer_gate("stranger")
        block.assert_not_called()
        # Review-only is about one's own PRs: it does not let the author re-request a fixer's.
        block, _ = self.reviewer_gate(OWNER, author="fixer")
        block.assert_not_called()

    def test_no_cap_on_a_review_only_pr(self):
        block, breach = self.reviewer_gate(OWNER, rounds=self.loop["cap"] + 1)
        breach.assert_not_called()
        block.assert_called_once()
        self.live = fg.LIVE                        # a fixer's PR at the cap is still escalated
        block, breach = self.reviewer_gate("fixer", rounds=self.loop["cap"], author="fixer")
        breach.assert_called_once()
        block.assert_not_called()

    def test_an_unlisted_author_is_not_reviewed(self):
        block, _ = self.reviewer_gate("outsider", author="outsider")
        block.assert_not_called()

    def test_the_reviewer_is_told_the_author_answers(self):
        row = {"seat": "reviewer", "repo": fg.REPO, "pr": 7, "head": HEAD}
        with mock.patch.object(gh, "issue_comments_read", return_value=([], "")):
            text = run_supervisor.isolated_prompt(
                self.loop, row, [], change=run_supervisor.PRChange("RECORD", "", author=OWNER))
            self.assertIn(f"you review, the PR's author ({OWNER}) fixes", text)
            fixer = run_supervisor.isolated_prompt(
                self.loop, row, [], change=run_supervisor.PRChange("RECORD", "", author="fixer"))
            self.assertNotIn("the PR's author", fixer)

    def explain(self):
        local = {"held": {}, "queued_seat": "", "queued_reason": "", "inflight_review": False,
                 "inflight_fix": False, "marker": {}, "parked": False, "delivery_status": "",
                 "capacity": {"reviewer": (0, 1), "fixer": (0, 1)}, "stale_queues": [],
                 "seat": "", "queue": "", "inflight": "", "escalation": "", "sweep": ""}
        with mock.patch.object(gate, "_explain_state", return_value=local):
            return gate.explain(self.loop, self.st, 7,
                                {"pr": self.live, "reviews": self.verdicts(self.loop["cap"]),
                                 "armed": True, "read_at": time.time()})

    def test_explain_hands_the_verdict_to_the_author(self):
        report = self.explain()
        self.assertEqual(report["next"]["kind"], "author-push")
        self.assertIn(OWNER, report["next"]["action"])
        self.assertIn(report["next"]["kind"], gate.EXPLAIN_KINDS)
        self.assertFalse(any("did not start one" in line or "cap may not have fired" in line
                             for line in report["blockers"]), report["blockers"])

    def sweep(self, reviews):
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=[self.live]), \
             mock.patch.object(gh, "pr", return_value=self.live), \
             mock.patch.object(gh, "reviews", return_value=reviews), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire") as fire, \
             mock.patch.object(watchdog.observer, "notify"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"):
            lines = watchdog.sweep_loop(self.loop, self.st)
        return "\n".join(lines), fire

    def aged_sweep(self, reviews):
        self.sweep(reviews)                                    # first sweep: arms, baselines
        watch = self.st.watch()
        watch["heads"]["7"]["observed_at"] = time.time() - 3600 * 24
        self.st.watch_save(watch)
        return self.sweep(reviews)

    def test_watchdog_no_fixer_stall_but_still_a_reviewer_stall(self):
        text, fire = self.aged_sweep(self.verdicts(self.loop["cap"] + 1))
        self.assertNotIn("fixer never pushed", text)
        self.assertNotIn("cap may not have fired", text)
        fire.assert_not_called()
        text, _ = self.aged_sweep([])
        self.assertIn("reviewer never posted a verdict", text)
        # The author pushed past more verdicts than the cap: still no escalation is missing.
        text, _ = self.aged_sweep(self.verdicts(self.loop["cap"] + 1, "c" * 40))
        self.assertNotIn("cap may not have fired", text)
        self.assertIn("reviewer never posted a verdict", text)

    def test_drain_serves_the_reviewer_seat_only(self):
        for seat in ("reviewer", "fixer"):
            self.st.queue_add(seat, f"{fg.REPO}#7", HEAD, "url", "queued")
        with mock.patch.object(gh, "pr", return_value=self.live), \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch.object(watchdog.routes, "fire", return_value=True) as fire:
            watchdog.drain(self.loop, self.st, "fixer", quiet=True)
            fire.assert_not_called()
            self.assertEqual(self.st.queue_items("fixer"), {})       # dropped, never fired
            watchdog.drain(self.loop, self.st, "reviewer", quiet=True)
        fire.assert_called_once()


LOOP_ID = "reviewonly"
LOOP_FILE = t.LOOPS_DIR / f"{LOOP_ID}.json"


class Cli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={})
        (t.LOOPS_DIR / "widgets.json").unlink(missing_ok=True)   # one loop per repo
        LOOP_FILE.unlink(missing_ok=True)
        self.addCleanup(LOOP_FILE.unlink, missing_ok=True)

    def cli(self, *argv, settings=None):
        return t.run_cli(t.parser_for(settings).parse_args(list(argv)))

    def init(self, *extra, settings=None):
        return self.cli("init", "--repo", t.REPO, "--id", LOOP_ID, "--host", t.HOST,
                        "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                        "--reviewer-profile", "reviewer-profile",
                        "--fixer-profile", "fixer-profile",
                        "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                        "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *t.READER_ARGS, *extra,
                        settings=settings)

    def written(self) -> list:
        return json.loads(LOOP_FILE.read_text()).get("review_only", [])

    def test_init_from_the_form_and_the_flags_win(self):
        form = {"review_only": "owner-human, other"}
        for extra, settings, want in (((), None, []), ((), form, [OWNER, "other"]),
                                      (("--review-only", "solo"), form, ["solo"])):
            with self.subTest(extra=extra, settings=settings):
                LOOP_FILE.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.written(), want)
        LOOP_FILE.unlink(missing_ok=True)
        rc, out = self.init("--review-only", t.FIXER)
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(LOOP_FILE.exists())

    def test_set_replaces_clears_and_refuses_and_apply_moves_it(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--review-only", OWNER,
                           "--review-only", "other")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), [OWNER, "other"])
        for bad in (("--review-only", "a", "--review-only", "a"), ("--review-only", t.FIXER),
                    ("--review-only", t.REVIEWER), ("--review-only", "not a login")):
            with self.subTest(bad=bad):
                rc, out = self.cli("set", "--loop", LOOP_ID, *bad)
                self.assertEqual(rc, 2, out)
                self.assertEqual(self.written(), [OWNER, "other"])
        rc, out = self.cli("set", "--loop", LOOP_ID, "--no-review-only")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), [])
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"review_only": "solo"})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("already matches", out)
        self.assertEqual(self.written(), ["solo"])

    def test_setup_hands_init_each_name(self):
        args = t.parser_for({"review_only": "owner-human, other"}
                            ).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn(f"--review-only={OWNER}", argv)
        self.assertIn("--review-only=other", argv)
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--review-only", "solo"])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--review-only")],
                         ["--review-only=solo"])


if __name__ == "__main__":
    unittest.main()
