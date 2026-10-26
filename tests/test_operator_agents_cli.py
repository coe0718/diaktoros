"""#757 `operator_agents`: the init / setup / set / apply paths write the loop file."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros.cli import install as cli_install  # noqa: E402
import run_tests as t  # noqa: E402
import test_human_paths as thp  # noqa: E402


class Cli(unittest.TestCase):
    setUpClass = classmethod(thp.Cli.setUpClass.__func__)
    tearDownClass = classmethod(thp.Cli.tearDownClass.__func__)
    setUp = thp.Cli.setUp
    cli = thp.Cli.cli
    init = thp.Cli.init

    def written(self) -> list:
        return thp.json.loads(thp.LOOP_FILE.read_text()).get("operator_agents", [])

    def test_init_from_the_form_and_the_flag_wins(self):
        for extra, settings, want in (
                ((), None, []),
                ((), {"operator_agents": "Docs-Bot, bug-hunter"}, ["docs-bot", "bug-hunter"]),
                (("--operator-agent", "Other"), {"operator_agents": "docs-bot"}, ["other"]),
                (("--operator-agent", "a,b"), None, ["a", "b"])):
            with self.subTest(extra=extra, settings=settings):
                thp.LOOP_FILE.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.written(), want)

    def test_init_refuses_a_seat_account(self):
        thp.LOOP_FILE.unlink(missing_ok=True)
        rc, out = self.init("--operator-agent", "dev-fixer")
        self.assertNotEqual(rc, 0, out)
        self.assertFalse(thp.LOOP_FILE.exists())

    def test_set_replaces_clears_and_refuses_and_apply_moves_it(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--operator-agent", "Docs-Bot")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), ["docs-bot"])
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--operator-agent", "dev-fixer")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.written(), ["docs-bot"])
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--operator-agent", "x", "--operator-agent", "x")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.written(), ["docs-bot"])
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--no-operator-agents")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), [])
        rc, out = self.cli("apply", "--loop", thp.LOOP_ID, settings={"operator_agents": "bot-a"})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), ["bot-a"])
        rc, out = self.cli("show", "--loop", thp.LOOP_ID)
        self.assertIn("bot-a", out)

    def test_setup_hands_init_each_agent(self):
        args = t.parser_for({"operator_agents": "a, b"}).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli_install._setup_init_argv(args, t.REPO, thp.LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--operator-agent")],
                         ["--operator-agent=a", "--operator-agent=b"])
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--operator-agent", "c"])
        argv, _ = cli_install._setup_init_argv(args, t.REPO, thp.LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--operator-agent")],
                         ["--operator-agent=c"])


if __name__ == "__main__":
    unittest.main()
