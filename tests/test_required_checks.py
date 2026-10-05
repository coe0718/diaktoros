"""Required checks the operator owns (#368): only they gate the approval and the CI hold.

An optional check (a bot, a coverage upload) is shown to the reviewer but never blocks, holds or
wakes anyone; a required check that has not reported at the head counts as running. The list is
host-owned and moves from every settings path.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
import test_ci_gate as cg  # noqa: E402
import test_seat_models as sm  # noqa: E402
from review_loop import ci, cli, config, observer  # noqa: E402
from review_loop.run_supervisor import CI_HOLD  # noqa: E402

REQUIRED = ["tests (3.11)", "test (ubuntu-24.04, 3.11)"]


def raw(**extra) -> dict:
    return {"repo": "acme/widgets", "fixers": ["fix"], "reviewers": ["rev"],
            "read_token": "reader", "tokens": {},
            "seats": {"reviewer": {"profile": "r", "login": "rev", "route": "w-review"},
                      "fixer": {"profile": "f", "login": "fix", "route": "w-fix"}}, **extra}


class Setting(unittest.TestCase):
    def test_default_none_and_validation(self):
        self.assertEqual(config.normalize(raw())["required_checks"], [])
        self.assertEqual(config.required_checks(config.normalize(raw(required_checks=REQUIRED))),
                         REQUIRED)
        for bad in ("tests", ["a", "a"], [""], ["x" * 101], ["a\nb"], [3],
                    [f"c{i}" for i in range(51)]):
            with self.subTest(value=bad), self.assertRaises(config.ConfigError):
                config.normalize(raw(required_checks=bad))

    def test_split_keeps_matrix_commas(self):
        self.assertEqual(config.split_check_names(" tests (3.11), lint ,, test (ubuntu-24.04, 3.11)"),
                         ["tests (3.11)", "lint", "test (ubuntu-24.04, 3.11)"])
        self.assertEqual(config.split_check_names(""), [])

    def test_the_form_moves_it_only_when_it_names_it(self):
        self.assertNotIn("required_checks", config.apply_settings(raw(), {}))
        got = config.apply_settings(raw(), {"required_checks": "tests (3.11), test (ubuntu-24.04, 3.11)"})
        self.assertEqual(got["required_checks"], REQUIRED)
        self.assertEqual(config.apply_settings(raw(required_checks=["a"]),
                                               {"required_checks": " "})["required_checks"], ["a"])


STATE = ci.CIState(failed=["flaky-bot"], passed=["tests (3.11)"], cancelled=["coverage"],
                   pending=["nightly"])


class Gating(unittest.TestCase):
    def test_no_list_means_every_check(self):
        self.assertIs(ci.gating(STATE, []), STATE)
        self.assertIsNone(ci.gating(None, REQUIRED))
        self.assertIsNone(ci.optional(STATE, []))

    def test_only_required_checks_gate_and_a_missing_one_is_running(self):
        view = ci.gating(STATE, REQUIRED)
        self.assertEqual((view.failed, view.cancelled, view.passed, view.pending),
                         ([], [], ["tests (3.11)"], ["test (ubuntu-24.04, 3.11)"]))
        self.assertEqual(ci.approval_refusal(view), "")             # optional red never blocks
        rest = ci.optional(STATE, REQUIRED)
        self.assertEqual((rest.failed, rest.cancelled, rest.pending),
                         (["flaky-bot"], ["coverage"], ["nightly"]))

    def test_a_required_failure_or_cancellation_still_refuses(self):
        red = ci.CIState(failed=["tests (3.11)"], passed=["test (ubuntu-24.04, 3.11)"])
        self.assertIn("CI has failed", ci.approval_refusal(ci.gating(red, REQUIRED)))
        cancelled = ci.CIState(cancelled=["tests (3.11)"], passed=["test (ubuntu-24.04, 3.11)"])
        self.assertIn("needs a re-run", ci.approval_refusal(ci.gating(cancelled, REQUIRED)))

    def test_section_separates_required_from_optional(self):
        text = ci.section(STATE, REQUIRED)
        required, optional = text.split("Optional checks", 1)
        self.assertIn("Required checks (these gate the approval)", required)
        self.assertIn('**not reported at this head yet:** "test (ubuntu-24.04, 3.11)"', required)
        self.assertNotIn("flaky-bot", required)
        self.assertIn('failed: "flaky-bot"', optional)
        self.assertIn("never block", optional)
        self.assertNotIn("Required checks", ci.section(STATE))       # no list: as before


class Broker(cg.Broker):
    """The live broker, with the loop naming its required checks."""

    def setUp(self):
        super().setUp()
        self.loop = {**self.loop, "required_checks": ["tests (3.11)"]}

    def test_an_optional_failure_approves_a_required_one_refuses(self):
        self.runs = [cg.run("tests (3.11)"), cg.run("flaky-bot", conclusion="failure")]
        sup, scope = self.ledgered("")
        self.assertTrue(self.send(self.start(scope, require_receipt=True), "APPROVE")["ok"])
        (self.root / "runs.sqlite").unlink()
        self.posts = []
        self.runs = [cg.run("tests (3.11)", conclusion="failure"), cg.run("flaky-bot")]
        sup, scope = self.ledgered("")
        refused = self.send(self.start(scope, require_receipt=True), "APPROVE")
        self.assertIn('CI has failed at this head ("tests (3.11)")', refused["error"])
        self.assertEqual(self.posts, [])


for _name in [n for n in dir(cg.Broker) if n.startswith("test_")]:
    setattr(Broker, _name, None)     # the base tests run in test_ci_gate


class Hold(sm.Worker):
    def held(self, checks, required):
        loop = {**self.loop, "review_after_ci": True, "required_checks": required}
        with mock.patch.object(ci, "read", return_value=checks), \
             mock.patch.object(observer, "notify"):
            seen, row, _ = self.run_seat("reviewer", loop=loop)
        return seen, row

    def test_only_required_checks_hold_the_review(self):
        checks = ci.CIState(passed=["tests (3.11)"], pending=["nightly"], cancelled=["coverage"])
        seen, row = self.held(checks, ["tests (3.11)"])
        self.assertEqual((seen["role"], row[0]), ("reviewer", "succeeded"))
        seen, row = self.held(checks, [])                            # no list: they all hold
        self.assertEqual(row[0], "waiting")
        seen, row = self.held(checks, ["tests (3.11)", "tests (3.14)"])   # one never reported
        self.assertEqual(seen, {})
        self.assertEqual(row, ("waiting", f"{CI_HOLD} on {sm.HEAD[:7]} — 1 check(s) still running "
                                          "or not reported"))


for _name in [n for n in dir(sm.Worker) if n.startswith("test_")]:
    setattr(Hold, _name, None)       # its own tests run in test_seat_models


LOOP_ID = "requiring"
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
        return json.loads(LOOP_FILE.read_text()).get("required_checks", [])

    def test_init_from_the_form_and_the_flags_win(self):
        form = {"required_checks": "tests (3.11), test (ubuntu-24.04, 3.11)"}
        for extra, settings, want in (((), None, []), ((), form, REQUIRED),
                                      (("--required-check", "lint"), form, ["lint"])):
            with self.subTest(extra=extra, settings=settings):
                LOOP_FILE.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.written(), want)

    def test_set_replaces_clears_and_refuses_and_apply_moves_it(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--required-check", "tests (3.11)",
                           "--required-check", "test (ubuntu-24.04, 3.11)")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), REQUIRED)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--required-check", "a",
                           "--required-check", "a")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.written(), REQUIRED)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--no-required-checks")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), [])
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"required_checks": "lint"})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("already matches", out)
        self.assertEqual(self.written(), ["lint"])

    def test_setup_hands_init_each_name(self):
        args = t.parser_for({"required_checks": "tests (3.11), test (ubuntu-24.04, 3.11)"}
                            ).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--required-check=tests (3.11)", argv)
        self.assertIn("--required-check=test (ubuntu-24.04, 3.11)", argv)
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--required-check", "lint"])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--required-check")],
                         ["--required-check=lint"])


if __name__ == "__main__":
    unittest.main()
