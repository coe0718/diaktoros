"""Review-only authors (#191): the reviewer reviews their PRs, the fixer never touches them.

A changes-requested verdict on a review-only author's PR goes back to that author: no fixer turn,
no adjudication, and no "fixer never pushed" stall; its verdicts have their own cap
(review_only_cap). The author may ask for the next review themselves below that cap.
The list moves from every settings path, and a login is never both a
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
from diaktoros import cli, config, gate, gh, run_supervisor  # noqa: E402
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


class DocsMatchTheCap(unittest.TestCase):
    """The docs must not say review-only PRs have no verdict cap (they have review_only_cap)."""

    def test_review_only_rows_do_not_claim_no_cap(self):
        docs = pathlib.Path(__file__).resolve().parent.parent / "docs"
        for name in ("configuration.md", "settings.md"):
            rows = [line for line in (docs / name).read_text().splitlines()
                    if line.startswith("| `review_only` |") and "whose PRs" in line
                    or line.startswith("| `review_only` |") and "never fixed" in line]
            self.assertEqual(len(rows), 1, name)
            self.assertNotIn("no verdict cap", rows[0], name)
            self.assertNotIn("no cap", rows[0], name)
            self.assertIn("review_only_cap", rows[0], name)


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

    def reviewer_gate(self, sender: str, rounds: int = 0, author: str = OWNER, notes=None):
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
             mock.patch.object(gate_reviewer.observer, "notify",
                               side_effect=lambda *a, **kw: (notes if notes is not None
                                                             else []).append(kw)), \
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

    def test_a_review_only_pr_below_its_cap_is_reviewed_and_a_fixer_pr_still_breaches(self):
        block, breach = self.reviewer_gate(OWNER, rounds=self.loop["cap"] - 1)
        breach.assert_not_called()
        block.assert_called_once()
        self.live = fg.LIVE                        # a fixer's PR at the cap is still escalated
        block, breach = self.reviewer_gate("fixer", rounds=self.loop["cap"], author="fixer")
        breach.assert_called_once()
        block.assert_not_called()

    def test_at_the_cap_the_next_review_is_declined_with_one_notice(self):
        cap, notes = self.loop["cap"], []
        for sender in (OWNER, OWNER, "outsider"):   # the author asking again is no bypass
            block, breach = self.reviewer_gate(sender, rounds=cap, notes=notes)
            block.assert_not_called()
            breach.assert_not_called()
        block, breach = self.reviewer_gate(OWNER, rounds=cap + 2, notes=notes)
        block.assert_not_called()
        said = [n for n in notes if "review cap reached" in n.get("outcome", "")]
        self.assertEqual(len(said), 1, notes)
        self.assertIn(f"review cap reached on #7 ({cap} verdicts)", said[0]["outcome"])
        self.assertIn("--another-round", said[0]["outcome"])
        self.assertEqual(self.ledger_rows(), [])

    def test_review_only_cap_overrides_the_loop_cap(self):
        path = config.config_dir() / "one.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "review_only_cap": 5}))
        block, _ = self.reviewer_gate(OWNER, rounds=self.loop["cap"])
        block.assert_called_once()
        block, _ = self.reviewer_gate(OWNER, rounds=5)
        block.assert_not_called()

    def test_another_round_allows_exactly_one_more_verdict(self):
        cap = self.loop["cap"]
        self.assertTrue(self.st.review_cap_grant(7, HEAD, cap))
        self.assertFalse(self.st.review_cap_grant(7, HEAD, cap))     # a replay changes nothing
        block, _ = self.reviewer_gate(OWNER, rounds=cap)
        block.assert_called_once()
        block, _ = self.reviewer_gate(OWNER, rounds=cap + 1)        # the verdict landed: spent
        block.assert_not_called()

    def test_another_round_belongs_to_the_head_it_was_granted_on(self):
        self.st.review_cap_grant(7, "d" * 40, self.loop["cap"])
        block, _ = self.reviewer_gate(OWNER, rounds=self.loop["cap"])
        block.assert_not_called()

    def test_explain_names_the_cap_and_the_command(self):
        report = self.explain()
        self.assertEqual(report["next"]["kind"], "operator")
        self.assertIn("review cap reached", report["next"]["action"])
        self.assertIn("--another-round", report["next"]["action"])
        self.st.review_cap_grant(7, HEAD, self.loop["cap"])
        self.assertEqual(self.explain()["next"]["kind"], "author-push")

    def review_cmd(self, reviews):
        args = mock.Mock(loop="one", pr=7, another_round=True)
        with mock.patch.object(gh, "pr", return_value=self.live), \
             mock.patch.object(gh, "reviews", return_value=reviews), \
             mock.patch.object(cli.subprocess, "run") as run, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = cli.cmd_review(args)
        return code, out.getvalue(), run

    def test_another_round_is_refused_on_a_head_that_already_has_a_verdict(self):
        cap = self.loop["cap"]
        code, out, run = self.review_cmd(self.verdicts(cap, HEAD))
        self.assertEqual(code, 1)
        self.assertIn("already has a verdict", out)
        self.assertNotIn("granted —", out)
        self.assertFalse(self.st.review_cap_granted(7, HEAD, cap))
        run.assert_not_called()
        self.assertIn("pushes a new head first", self.explain(cap)["next"]["action"])

    def test_another_round_is_granted_on_a_new_unreviewed_head(self):
        cap = self.loop["cap"]
        with mock.patch.object(cli, "_reviewer_runs", return_value=0):
            code, out, _ = self.review_cmd(self.verdicts(cap, "c" * 40))
        self.assertIn("another round granted", out)
        self.assertTrue(self.st.review_cap_granted(7, HEAD, cap))

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

    def explain(self, verdicts=None):
        local = {"held": {}, "queued_seat": "", "queued_reason": "", "inflight_review": False,
                 "inflight_fix": False, "marker": {}, "parked": False, "delivery_status": "",
                 "capacity": {"reviewer": (0, 1), "fixer": (0, 1)}, "stale_queues": [],
                 "seat": "", "queue": "", "inflight": "", "escalation": "", "sweep": ""}
        with mock.patch.object(gate, "_explain_state", return_value=local):
            return gate.explain(self.loop, self.st, 7,
                                {"pr": self.live, "reviews": self.verdicts(self.loop["cap"] if verdicts is None
                                                         else verdicts),
                                 "armed": True, "read_at": time.time()})

    def test_explain_hands_the_verdict_to_the_author(self):
        report = self.explain(self.loop["cap"] - 1)
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
        # Below the cap an unreviewed new head is still a reviewer stall.
        text, _ = self.aged_sweep(self.verdicts(self.loop["cap"] - 1, "c" * 40))
        self.assertNotIn("cap may not have fired", text)
        self.assertIn("reviewer never posted a verdict", text)
        # At the cap no review is coming: the watchdog stays quiet (a maintainer's move).
        text, fire = self.aged_sweep(self.verdicts(self.loop["cap"], "c" * 40))
        self.assertNotIn("cap may not have fired", text)
        self.assertNotIn("reviewer never posted a verdict", text)
        fire.assert_not_called()

    # #478 human_paths: a head whose approval was left to a person is not reviewed again, is
    # explained as the operator's move, and is not a stall. A new head is reviewed afresh.
    def test_human_paths_hold_stops_the_gate_at_that_head_only(self):
        self.st.human_hold_set(7, HEAD, "it touches .github/ci.yml")
        block, _ = self.reviewer_gate(OWNER)
        block.assert_not_called()
        self.st.human_hold_set(7, "d" * 40, "an older head")
        block, _ = self.reviewer_gate(OWNER)
        block.assert_called_once()

    def test_human_paths_hold_is_explained_as_a_persons_move(self):
        self.st.human_hold_set(7, HEAD, "it touches .github/ci.yml")
        report = self.explain(0)
        self.assertEqual(report["next"]["kind"], "operator")
        self.assertIn("a person must review and approve", report["next"]["action"])
        self.assertIn(".github/ci.yml", report["next"]["action"])

    def test_human_paths_hold_is_not_a_reviewer_stall(self):
        self.st.human_hold_set(7, HEAD, "it touches .github/ci.yml")
        text, _ = self.aged_sweep([])
        self.assertNotIn("reviewer never posted a verdict", text)

    def test_a_conflict_goes_to_the_author_and_queues_no_fixer_turn(self):
        self.live = {**self.live, "mergeable_state": "dirty", "base": {"ref": "main", "sha": "b" * 40}}
        with mock.patch.object(gate, "enqueue_isolated") as enqueue, \
             mock.patch.object(watchdog.observer, "notify") as notify, \
             mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=[self.live]), \
             mock.patch.object(gh, "pr", return_value=self.live), \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"):
            watchdog.sweep_loop(self.loop, self.st)          # first sweep: arms, baselines
            watchdog.sweep_loop(self.loop, self.st)
        enqueue.assert_not_called()
        conflict = [c for c in notify.call_args_list if c.args[2] == "conflict"]
        self.assertEqual(len(conflict), 1)
        self.assertIn(f"{OWNER}: merge main", conflict[0].kwargs["next_turn"])

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

    def limits(self) -> tuple:
        data = json.loads(LOOP_FILE.read_text())
        return data.get("review_only_cap"), data.get("review_only_daily")

    def test_the_limits_move_from_every_settings_path(self):
        form = {"review_only_cap": "4", "review_only_daily": "6"}
        rc, out = self.init(settings=form)                       # init takes the form
        self.assertEqual((rc, self.limits()), (0, (4, 6)), out)
        LOOP_FILE.unlink()
        rc, out = self.init("--review-only-cap", "2", "--review-only-daily", "9", settings=form)
        self.assertEqual((rc, self.limits()), (0, (2, 9)), out)  # flags win
        rc, out = self.cli("set", "--loop", LOOP_ID, "--review-only-cap", "5",
                           "--review-only-daily", "7")
        self.assertEqual((rc, self.limits()), (0, (5, 7)), out)  # set
        rc, out = self.cli("set", "--loop", LOOP_ID, "--review-only-daily", "0")
        self.assertEqual((rc, self.limits()), (0, (5, None)), out)   # 0 clears the daily cap
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings=form)
        self.assertEqual((rc, self.limits()), (0, (4, 6)), out)  # apply pushes the form
        self.assertNotIn("review_only_cap", config.apply_settings(raw(), {}))
        schema = (t.ROOT / "plugin.yaml").read_text()
        for key in ("review_only_cap", "review_only_daily"):
            self.assertIn(f"  {key}:", schema)                   # the Desktop form

    def test_the_limits_are_validated_as_positive_integers(self):
        for bad in ("0", "-1", "x", "1.5", "1001"):
            with self.subTest(value=bad):
                LOOP_FILE.unlink(missing_ok=True)
                for flag in ("--review-only-cap", "--review-only-daily"):
                    rc, out = self.init(flag, bad)
                    self.assertNotEqual(rc, 0, out)
                    self.assertFalse(LOOP_FILE.exists())
                self.assertEqual(self.init()[0], 0)
                rc, out = self.cli("set", "--loop", LOOP_ID, "--review-only-cap", bad) \
                    if bad != "0" else (2, "")
                self.assertEqual(rc, 2, out)
                for key in ("review_only_cap", "review_only_daily"):
                    with self.assertRaises(config.ConfigError):
                        config.apply_settings(raw(), {key: bad})
                    with self.assertRaises(config.ConfigError):
                        config.normalize(raw(**{key: int(bad) if bad.lstrip("-").isdigit() else bad}))
        loop = config.normalize(raw())
        self.assertEqual((config.review_only_cap(loop), config.review_only_daily(loop)),
                         (loop["cap"], None))
        for blank in ("", None):
            self.assertIsNone(config.normalize(raw(review_only_daily=blank))["review_only_daily"])

    def test_setup_hands_init_the_limits(self):
        args = t.parser_for({"review_only_cap": "4"}).parse_args(
            ["setup", "--repo", t.REPO, "--review-only-daily", "6"])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--review-only-daily=6", argv)

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
