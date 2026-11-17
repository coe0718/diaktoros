"""Main stays at zero lint findings: CI's `lint` job runs a pinned ruff with ruff.toml's rules, and
fails on any finding. Ruff is not a test dependency (the tests are stdlib-only), so this reads the
workflow and the config as text: the job exists, it checks the whole tree with the pinned
release, and the rules and the one exemption are the ones agreed."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import re
import tomllib
import unittest

from test_ci_timeouts import WORKFLOW, jobs

ROOT = pathlib.Path(__file__).resolve().parents[1]


class LintJob(unittest.TestCase):
    def test_ci_runs_a_pinned_ruff_over_the_whole_tree(self):
        lint = "\n".join(jobs(WORKFLOW.read_text()).get("lint", []))
        self.assertRegex(lint, r"pip install -q ruff==\d+\.\d+\.\d+\n")
        self.assertRegex(lint, r"\n\s+ruff check (--output-format github )?\.$")

    def test_the_rules_are_pyflakes_and_pycodestyles_import_statement_and_syntax_checks(self):
        config = tomllib.loads((ROOT / "ruff.toml").read_text())
        self.assertEqual(config["lint"]["select"], ["E4", "E7", "E9", "F"])
        # The one exemption: the harness's shared star-import namespace, and only its F405.
        self.assertEqual(config["lint"]["per-file-ignores"], {"tests/harness/*.py": ["F405"]})
        self.assertNotIn("ignore", config["lint"])
        text = (ROOT / "ruff.toml").read_text()
        self.assertFalse(re.search(r"^\s*(exclude|extend-exclude)\s*=", text, re.M))


if __name__ == "__main__":
    unittest.main()
