"""#756 `ci_coverage`: the init / setup / set / apply / show paths write and report the map."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros.cli import setup_command as cli_setup  # noqa: E402
import run_tests as t  # noqa: E402
import test_human_paths as thp  # noqa: E402

ENTRY = "tests (3.14)=full unit suite and harness"
WANT = {"tests (3.14)": "full unit suite and harness"}


class Cli(unittest.TestCase):
    setUpClass = classmethod(thp.Cli.setUpClass.__func__)
    tearDownClass = classmethod(thp.Cli.tearDownClass.__func__)
    setUp = thp.Cli.setUp
    cli = thp.Cli.cli
    init = thp.Cli.init

    def written(self) -> dict:
        return thp.json.loads(thp.LOOP_FILE.read_text()).get("ci_coverage", {})

    def test_init_from_the_form_and_the_flag_wins(self):
        for extra, settings, want in (
                ((), None, {}), ((), {"ci_coverage": ENTRY}, WANT),
                ((), {"ci_coverage": ENTRY + " ;; lint=style rules"},
                 {**WANT, "lint": "style rules"}),
                (("--ci-covers", "lint=style"), {"ci_coverage": ENTRY}, {"lint": "style"})):
            with self.subTest(extra=extra, settings=settings):
                thp.LOOP_FILE.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.written(), want)

    def test_init_refuses_a_bad_entry(self):
        for bad in ("no-equals", "=text", "a=", "a=b\nc", "diaktoros/review=x"):
            with self.subTest(bad=bad):
                thp.LOOP_FILE.unlink(missing_ok=True)
                try:
                    rc, out = self.init("--ci-covers", bad)
                except Exception as exc:  # ConfigError escaping init is also a refusal
                    self.assertIn("ci_coverage", str(exc))
                else:
                    self.assertNotEqual(rc, 0, out)
                self.assertFalse(thp.LOOP_FILE.exists())

    def test_set_replaces_clears_and_refuses_and_apply_moves_it(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--ci-covers", ENTRY)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), WANT)
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--ci-covers", "x=a", "--ci-covers", "x=b")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.written(), WANT)
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--no-ci-coverage")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), {})
        rc, out = self.cli("apply", "--loop", thp.LOOP_ID, settings={"ci_coverage": ENTRY})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), WANT)
        rc, out = self.cli("show", "--loop", thp.LOOP_ID)
        self.assertIn("full unit suite and harness", out)

    def test_setup_hands_init_each_entry(self):
        args = t.parser_for({"ci_coverage": ENTRY + " ;; b=more"}
                            ).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, thp.LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--ci-covers")],
                         [f"--ci-covers={ENTRY}", "--ci-covers=b=more"])
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--ci-covers", "c=d"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, thp.LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--ci-covers")], ["--ci-covers=c=d"])


if __name__ == "__main__":
    unittest.main()
