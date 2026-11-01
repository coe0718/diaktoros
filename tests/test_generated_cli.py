"""#551 `generated`: the init / setup / set / apply argv paths write the loop file (F1)."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros.cli import setup_command as cli_setup  # noqa: E402
import run_tests as t  # noqa: E402
import test_human_paths as thp  # noqa: E402

ENTRY = "docs/commands.md=python3 tests/commands_doc.py --write"
WANT = [{"paths": ["docs/commands.md"], "command": "python3 tests/commands_doc.py --write"}]


class Cli(unittest.TestCase):
    setUpClass = classmethod(thp.Cli.setUpClass.__func__)
    tearDownClass = classmethod(thp.Cli.tearDownClass.__func__)
    setUp = thp.Cli.setUp
    cli = thp.Cli.cli
    init = thp.Cli.init

    def written(self) -> list:
        return thp.json.loads(thp.LOOP_FILE.read_text()).get("generated", [])

    def test_init_from_the_form_and_the_flag_wins(self):
        other = "a/b.txt,a/c.txt=make gen"
        for extra, settings, want in (
                ((), None, []), ((), {"generated": ENTRY}, WANT),
                (("--generated", other), {"generated": ENTRY},
                 [{"paths": ["a/b.txt", "a/c.txt"], "command": "make gen"}])):
            with self.subTest(extra=extra, settings=settings):
                thp.LOOP_FILE.unlink(missing_ok=True)
                rc, out = self.init(*extra, settings=settings)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.written(), want)

    def test_init_refuses_a_bad_entry(self):
        for bad in ("no-equals", "../x=cmd", "a=", "a=b\nc"):
            with self.subTest(bad=bad):
                thp.LOOP_FILE.unlink(missing_ok=True)
                try:
                    rc, out = self.init("--generated", bad)
                except Exception as exc:  # ConfigError escaping init is also a refusal
                    self.assertIn("generated", str(exc))
                else:
                    self.assertNotEqual(rc, 0, out)
                self.assertFalse(thp.LOOP_FILE.exists())

    def test_set_replaces_clears_and_refuses_and_apply_moves_it(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--generated", ENTRY)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), WANT)
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--generated", "x=a", "--generated", "x=b")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.written(), WANT)
        rc, out = self.cli("set", "--loop", thp.LOOP_ID, "--no-generated")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), [])
        rc, out = self.cli("apply", "--loop", thp.LOOP_ID, settings={"generated": ENTRY})
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.written(), WANT)
        rc, out = self.cli("show", "--loop", thp.LOOP_ID)
        self.assertIn("tests/commands_doc.py --write", out)

    def test_setup_hands_init_each_entry(self):
        args = t.parser_for({"generated": ENTRY + " ;; b.txt=make b"}
                            ).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, thp.LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--generated")],
                         [f"--generated={ENTRY}", "--generated=b.txt=make b"])
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO, "--generated", "c=d"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, thp.LOOP_ID, interactive=False)
        self.assertEqual([a for a in argv if a.startswith("--generated")], ["--generated=c=d"])


if __name__ == "__main__":
    unittest.main()
