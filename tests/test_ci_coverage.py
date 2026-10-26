"""#756 ``ci_coverage``: the reviewer is told what each green required check covers.

The reviewer's approval of #752 listed the full unit suite and the harness as "not verified"
though the required ``tests`` job had run both, green, at that head. The operator maps a check to
what it covers; the host reads CI itself and puts the mapped checks that are green at the head
into the reviewer's prompt, so a model cannot claim a check green. A red, running, cancelled,
missing or optional one is listed as not citable; with no map the prompt is unchanged.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _ci_green  # noqa: E402
import test_adjudicator_isolated as tai  # noqa: E402  (REPO, HEAD)
import test_required_checks as trc  # noqa: E402
from diaktoros import ci, config, gh, issue_facts  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402

MAP = {"tests (3.14)": "full unit suite and harness"}
HEADING = "## What the CI checks at this head cover"


class Setting(unittest.TestCase):
    def test_default_validation_and_spellings(self):
        self.assertEqual(config.normalize(trc.raw())["ci_coverage"], {})
        self.assertEqual(config.ci_coverage(config.normalize(trc.raw(ci_coverage=MAP))), MAP)
        for bad in ("tests", ["a"], {"": "x"}, {"a": ""}, {"a": 3}, {"a=b": "x"}, {"a": "x;;y"},
                    {"a\nb": "x"}, {"x" * 101: "y"}, {"a": "y" * 201}, {"diaktoros/review": "x"},
                    {f"c{i}": "x" for i in range(51)}, {"a": "x", " a": "y"}):
            with self.subTest(value=bad), self.assertRaises(config.ConfigError):
                config.normalize(trc.raw(ci_coverage=bad))
        self.assertEqual(config.parse_ci_coverage(
            config.split_ci_coverage("tests (3.14)=full suite ;; lint=style"), "t"),
            {"tests (3.14)": "full suite", "lint": "style"})
        self.assertEqual(config.ci_coverage_text({"a": "b", "c": "d"}), "a=b ;; c=d")
        with self.assertRaises(config.ConfigError):
            config.parse_ci_coverage(["no equals sign"], "t")
        with self.assertRaises(config.ConfigError):
            config.parse_ci_coverage(["a=b", "a=c"], "t")

    def test_the_form_moves_it_only_when_it_names_it(self):
        self.assertNotIn("ci_coverage", settings_form.apply_settings(trc.raw(), {}))
        got = settings_form.apply_settings(
            trc.raw(), {"ci_coverage": "tests (3.14)=full unit suite and harness"})
        self.assertEqual(got["ci_coverage"], MAP)
        self.assertEqual(settings_form.apply_settings(
            trc.raw(ci_coverage=MAP), {"ci_coverage": " "})["ci_coverage"], MAP)


class View(unittest.TestCase):
    def test_only_a_green_gating_check_is_cited(self):
        state = ci.CIState(passed=["tests (3.14)", "lint"], failed=["red"], pending=["slow"],
                           cancelled=["gone"])
        covers = {"tests (3.14)": "suite", "red": "r", "slow": "s", "gone": "g", "absent": "a",
                  "lint": "style"}
        green, other = ci.coverage_view(state, [], covers)
        self.assertEqual(set(green), {"tests (3.14)", "lint"})
        self.assertEqual(other, {"red": "failed", "slow": "still running", "gone": "cancelled",
                                 "absent": "not reported at this head"})
        # With required checks named, an optional green one is not citable either.
        green, other = ci.coverage_view(state, ["tests (3.14)", "red", "absent"], covers)
        self.assertEqual(set(green), {"tests (3.14)"})
        self.assertIn("not a required check", other["lint"])
        self.assertEqual(other["absent"], "not reported at this head")
        # CI that cannot be read cites nothing.
        self.assertEqual(ci.coverage_view(None, [], covers)[0], {})

    def test_no_map_is_no_section(self):
        self.assertEqual(ci.coverage_section(ci.CIState(passed=["a"]), [], {}), "")
        self.assertEqual(ci.coverage_section(ci.CIState(passed=["a"]), [], None), "")

    def test_the_section_splits_green_from_the_rest(self):
        state = ci.CIState(passed=["tests (3.14)"], failed=["lint"])
        text = ci.coverage_section(state, [], {**MAP, "lint": "style rules"})
        self.assertIn(HEADING, text)
        self.assertIn('- "tests (3.14)": full unit suite and harness', text)
        self.assertIn('- "lint" (failed): style rules', text)
        self.assertIn("Ran in the sandbox", text)
        self.assertIn("Not run anywhere", text)
        red = ci.coverage_section(ci.CIState(failed=["tests (3.14)"]), [], MAP)
        self.assertIn("No covering check is green", red)
        self.assertIn('"tests (3.14)" (failed)', red)
        self.assertIn("could not be read",
                      ci.coverage_section(None, [], MAP))


def reviewer_prompt(loop, state, seat="reviewer"):
    """The real ``isolated_prompt`` for a PR at HEAD, with GitHub reads and CI mocked."""
    row = {"seat": seat, "repo": tai.REPO, "pr": 7, "head": tai.HEAD}
    change = mock.Mock(record="CHANGE-RECORD", diff="")
    with mock.patch.object(gh, "issue_comments_read", return_value=([], "")), \
            mock.patch.object(ci, "read", return_value=state), \
            mock.patch.object(issue_facts, "section", return_value=""), \
            mock.patch("diaktoros.ci_tests.read", return_value={}), \
            mock.patch("diaktoros.ci_fix.fix_history", return_value=""), \
            mock.patch("diaktoros.ci_fix.still_red", return_value=""), \
            mock.patch.object(turn_prompts, "reproduction_section", return_value=""), \
            mock.patch("diaktoros.run_supervisor.pr_facts.pr_record", return_value="RECORD"):
        return turn_prompts.isolated_prompt(loop, row, [], None, change)


class Enforcing(unittest.TestCase):
    """Driven through ``turn_prompts.isolated_prompt``, the prompt the reviewer seat receives."""

    def loop(self, **extra):
        return config.normalize({**trc.raw(**extra), "id": "w"})

    def setUp(self):
        patch = mock.patch.object(gh, "api", side_effect=_ci_green.green(lambda *a, **k: None))
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_green_covering_check_is_handed_to_the_reviewer(self):
        state = ci.CIState(passed=["tests (3.14)"])
        text = reviewer_prompt(self.loop(ci_coverage=MAP), state)
        self.assertIn(HEADING, text)
        self.assertIn("Green at this head, covering:", text)
        self.assertIn('- "tests (3.14)": full unit suite and harness', text)

    def test_a_red_or_missing_covering_check_is_not_cited(self):
        for state, why in ((ci.CIState(failed=["tests (3.14)"]), "failed"),
                           (ci.CIState(pending=["tests (3.14)"]), "still running"),
                           (ci.CIState(passed=["other"]), "not reported at this head")):
            with self.subTest(why=why):
                text = reviewer_prompt(self.loop(ci_coverage=MAP), state)
                self.assertIn("No covering check is green", text)
                self.assertNotIn("Green at this head, covering:", text)
                self.assertIn(f'"tests (3.14)" ({why})', text)

    def test_an_unreadable_head_cites_nothing(self):
        text = reviewer_prompt(self.loop(ci_coverage=MAP), None)
        self.assertIn("No covering check is green", text)
        self.assertIn("could not be read", text)

    def test_with_no_map_the_prompt_is_unchanged(self):
        state = ci.CIState(passed=["tests (3.14)"])
        text = reviewer_prompt(self.loop(), state)
        self.assertNotIn(HEADING, text)
        self.assertNotIn("Green at this head", text)

    def test_only_the_reviewer_gets_it(self):
        state = ci.CIState(passed=["tests (3.14)"])
        loop = self.loop(ci_coverage=MAP)
        self.assertNotIn(HEADING, reviewer_prompt(loop, state, seat="fixer"))


class Template(unittest.TestCase):
    def test_the_reviewer_template_explains_the_split(self):
        from diaktoros import prompts
        text = " ".join(prompts.ISOLATED_REVIEWER.split())
        self.assertIn("What the CI checks at this head cover", text)
        self.assertIn("CI at this head", text)


if __name__ == "__main__":
    unittest.main()
