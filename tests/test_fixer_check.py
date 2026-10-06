"""The loop's always-run check: one command fixer turns run before publishing.

A fixer runs only the tests its change touches, so suite-wide checks (a guard test, a lint) went
red in CI after it published. The operator names those once, from any path (form, init, setup,
set, loop file), and fixer and issue-fix prompts carry it.
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
from review_loop import cli, config, prompts, run_supervisor  # noqa: E402

CHECK = 'python3 -m unittest discover -s tests -p "test_home_guard.py" && echo "${HOME}"'


def raw(**extra) -> dict:
    return {"repo": "acme/widgets", "fixers": ["fix"], "reviewers": ["rev"],
            "read_token": "reader", "tokens": {},
            "seats": {"reviewer": {"profile": "r", "login": "rev", "route": "w-review"},
                      "fixer": {"profile": "f", "login": "fix", "route": "w-fix"}}, **extra}


class Setting(unittest.TestCase):
    def test_default_is_none_and_only_one_printable_line_is_accepted(self):
        self.assertEqual(config.normalize(raw())["fixer_check"], "")
        self.assertEqual(config.normalize(raw(fixer_check=f"  {CHECK} "))["fixer_check"], CHECK)
        for bad in (["a"], 3, "a\nb", "a\x1bb", "x" * (config.FIXER_CHECK_MAX + 1)):
            with self.subTest(value=bad), self.assertRaises(config.ConfigError):
                config.normalize(raw(fixer_check=bad))

    def test_the_form_moves_it_only_when_it_names_it(self):
        self.assertNotIn("fixer_check", config.apply_settings(raw(), {}))
        self.assertEqual(config.apply_settings(raw(fixer_check="make lint"),
                                               {"fixer_check": " "})["fixer_check"], "make lint")
        self.assertEqual(config.apply_settings(raw(), {"fixer_check": CHECK})["fixer_check"],
                         CHECK)
        with self.assertRaises(config.ConfigError):
            config.apply_settings(raw(), {"fixer_check": "a\nb"})


class Prompts(unittest.TestCase):
    def test_none_adds_nothing(self):
        self.assertEqual(prompts.fixer_check_section({}), "")
        self.assertEqual(prompts.fixer_check_section({"fixer_check": "  "}), "")

    def test_fixer_and_issue_fix_prompts_carry_it_braces_and_all(self):
        loop = {"id": "w", "repo": "acme/widgets", "base": "main", "cap": 3,
                "fixer_check": CHECK, "seats": {}}
        row = {"seat": "fixer", "repo": "acme/widgets", "pr": 7, "head": "a" * 40}
        change = run_supervisor.PRChange("RECORD", "DIFF")
        with mock.patch("review_loop.gate.verdicts", return_value=[{}]), \
             mock.patch("review_loop.gate.latest_effective_review_at_head", return_value=None), \
             mock.patch("review_loop.gh.pr_url", return_value="https://github.com/acme/widgets/pull/7"), \
             mock.patch("review_loop.gh.issue_comments_read", return_value=([], None)):
            fixer = run_supervisor.isolated_prompt(loop, row, [], change=change)
        with mock.patch.object(run_supervisor, "issue_fix_issue",
                               return_value={"title": "T", "body": "B"}):
            issue = run_supervisor.issue_fix_prompt(loop, {**row, "seat": "issue_fixer"})
        for text in (fixer, issue):
            self.assertIn("## Always-run check (set by the loop's operator)", text)
            self.assertIn(f"```sh\n{CHECK}\n```", text)
            self.assertIn("If an **always-run check**", text)
            self.assertIn("Set `CHANGED` to every path you changed", text)          # #362
        # Instruction from the operator, before the data sections.
        self.assertLess(fixer.index("Always-run check"), fixer.index("RECORD"))
        self.assertLess(issue.index("Always-run check"), issue.index("## Issue"))


LOOP_ID = "checked"
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
        # The harness's own loop is for the same repository, and a repo has one loop.
        (t.LOOPS_DIR / "widgets.json").unlink(missing_ok=True)
        LOOP_FILE.unlink(missing_ok=True)
        self.addCleanup(LOOP_FILE.unlink, missing_ok=True)

    def cli(self, *argv, settings=None) -> tuple[int, str]:
        return t.run_cli(t.parser_for(settings).parse_args(list(argv)))

    def init(self, *extra, settings=None) -> tuple[int, str]:
        return self.cli("init", "--repo", t.REPO, "--id", LOOP_ID, "--host", t.HOST,
                        "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                        "--reviewer-profile", "reviewer-profile",
                        "--fixer-profile", "fixer-profile",
                        "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                        "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *t.READER_ARGS, *extra,
                        settings=settings)

    def written(self) -> dict:
        return json.loads(LOOP_FILE.read_text())

    def test_init_starts_from_the_form_and_the_flag_wins(self):
        rc, out = self.init()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written().get("fixer_check", ""), "")
        LOOP_FILE.unlink()
        rc, out = self.init(settings={"fixer_check": "make lint"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written().get("fixer_check", ""), "make lint")
        LOOP_FILE.unlink()
        rc, out = self.init("--fixer-check", CHECK, settings={"fixer_check": "make lint"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written().get("fixer_check", ""), CHECK)
        LOOP_FILE.unlink()
        rc, out = self.init("--fixer-check", "", settings={"fixer_check": "make lint"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written().get("fixer_check", ""), "")

    def test_set_names_it_clears_it_and_refuses_two_lines(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--fixer-check", CHECK)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written().get("fixer_check", ""), CHECK)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--fixer-check", "a\nb")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.written().get("fixer_check", ""), CHECK)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--fixer-check", "")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written().get("fixer_check", ""), "")

    def test_apply_saves_a_form_change_to_it_alone(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"fixer_check": "make lint"})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("already matches", out)
        self.assertEqual(self.written().get("fixer_check", ""), "make lint")
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"fixer_check": "make lint"})
        self.assertIn("already matches", out)

    def test_setup_hands_init_the_check(self):
        args = t.parser_for({"fixer_check": "make lint"}).parse_args(
            ["setup", "--repo", t.REPO, "--yes"])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--fixer-check=make lint", argv)
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--yes",
                                            "--fixer-check", CHECK])
        argv, _ = cli._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn(f"--fixer-check={CHECK}", argv)


if __name__ == "__main__":
    unittest.main()
