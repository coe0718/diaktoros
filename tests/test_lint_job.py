"""Main stays at zero lint findings: CI's `lint` job runs a pinned ruff with ruff.toml's rules, and
fails on any finding.

Two halves. The workflow is read as text (the tests are stdlib-only, so no YAML parser): the job
exists, nothing disables it or masks its failure, it checks the whole tree with the pinned
release, and it runs the execution half with ruff required. The execution half runs ruff, with a
copy of ruff.toml, on a scratch tree: a clean file passes, a finding fails, and the harness's
F405 exemption covers only F405 and only under tests/harness/. Without ruff it skips, except under
``DIAKTOROS_REQUIRE_RUFF=1`` (CI's lint job), where a missing ruff fails it.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest

from test_ci_timeouts import WORKFLOW, jobs

ROOT = pathlib.Path(__file__).resolve().parents[1]
REQUIRED = os.environ.get("DIAKTOROS_REQUIRE_RUFF") == "1"


def lint_job() -> str:
    return "\n".join(jobs(WORKFLOW.read_text()).get("lint", []))


class LintJob(unittest.TestCase):
    def test_ci_runs_a_pinned_ruff_over_the_whole_tree(self):
        lint = lint_job()
        self.assertRegex(lint, r"pip install -q ruff==\d+\.\d+\.\d+\n")
        self.assertRegex(lint, r"\n\s+ruff check (--output-format github )?\.\n")

    def test_nothing_disables_the_job_or_masks_its_failure(self):
        lint = lint_job()
        self.assertTrue(lint)
        # No condition (a job- or step-level `if:` can skip it), no continue-on-error, and no
        # shell-level escape hatch on a run line.
        self.assertNotRegex(lint, r"(?m)^\s*(-\s+)?if\s*:")
        self.assertNotIn("continue-on-error", lint)
        self.assertNotRegex(lint, r"\|\|\s*(true|:|exit 0)")
        self.assertNotRegex(lint, r"--exit-zero")

    def test_ci_runs_the_execution_check_with_ruff_required(self):
        lint = lint_job()
        self.assertRegex(lint, r'DIAKTOROS_REQUIRE_RUFF:\s*"1"')
        self.assertRegex(lint, r"python -m unittest discover -s tests -p test_lint_job\.py")

    def test_the_rules_are_pyflakes_and_pycodestyles_import_statement_and_syntax_checks(self):
        config = tomllib.loads((ROOT / "ruff.toml").read_text())
        self.assertEqual(config["lint"]["select"], ["E4", "E7", "E9", "F"])
        # The one exemption: the harness's shared star-import namespace, and only its F405.
        self.assertEqual(config["lint"]["per-file-ignores"], {"tests/harness/*.py": ["F405"]})
        self.assertNotIn("ignore", config["lint"])
        text = (ROOT / "ruff.toml").read_text()
        self.assertFalse(re.search(r"^\s*(exclude|extend-exclude)\s*=", text, re.M))


# A file that uses a name only a star import could supply: F405 wherever it is.
STAR = "from .fixture import *  # noqa: F403\n\n\ndef area():\n    return shared\n"


class LintRuns(unittest.TestCase):
    """ruff.toml's rules, executed: what CI's lint job enforces is what this sees."""

    def setUp(self):
        self.ruff = shutil.which("ruff")
        if self.ruff is None:
            if REQUIRED:
                self.fail("DIAKTOROS_REQUIRE_RUFF=1 but ruff is not on PATH")
            self.skipTest("ruff is not installed (CI's lint job runs this with it)")
        self.tree = pathlib.Path(tempfile.mkdtemp(prefix="lint-"))
        self.addCleanup(shutil.rmtree, self.tree)
        shutil.copyfile(ROOT / "ruff.toml", self.tree / "ruff.toml")

    def check(self, files: dict[str, str]) -> subprocess.CompletedProcess:
        for name, text in files.items():
            path = self.tree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return subprocess.run([self.ruff, "check", "--no-cache", "--output-format", "concise", "."],
                              cwd=self.tree, capture_output=True, text=True, timeout=60)

    def test_a_clean_tree_passes(self):
        result = self.check({"clean.py": "import os\n\nprint(os.sep)\n"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_finding_fails_the_check(self):
        for rule, text in (("F401", "import os\n"),
                           ("E741", "l = 1\nprint(l)\n"),
                           ("E731", "f = lambda: 1\nprint(f())\n")):
            with self.subTest(rule=rule):
                result = self.check({f"{rule.lower()}.py": text})
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(rule, result.stdout)
                (self.tree / f"{rule.lower()}.py").unlink()

    def test_the_harness_exemption_covers_only_f405_under_tests_harness(self):
        harness = self.check({"tests/harness/area.py": STAR})
        self.assertEqual(harness.returncode, 0, harness.stdout + harness.stderr)
        # The same file elsewhere is a finding...
        elsewhere = self.check({"diaktoros/area.py": STAR})
        self.assertEqual(elsewhere.returncode, 1, elsewhere.stdout)
        self.assertIn("diaktoros/area.py", elsewhere.stdout)
        self.assertIn("F405", elsewhere.stdout)
        (self.tree / "diaktoros" / "area.py").unlink()
        # ...and under tests/harness/ every other rule still applies.
        unused = self.check({"tests/harness/area.py": "import os\n" + STAR})
        self.assertEqual(unused.returncode, 1, unused.stdout)
        self.assertIn("F401", unused.stdout)


if __name__ == "__main__":
    unittest.main()
