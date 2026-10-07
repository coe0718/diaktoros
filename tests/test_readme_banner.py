"""The front page's status banner agrees with its own first-run steps (#85 part 2).

The banner once said the seats were disabled and the runtime file should stay absent, while the
numbered "First run" steps told the operator to create that file — the one switch (with `arm`)
that turns a queued event into a real seat turn that posts a review. A reader could not tell
from the front page whether seat turns post. These checks pin the two together.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = "diaktoros-runtime.json"
STALE = (r"disabled in this branch", r"worker config absent", r"Do not install this branch",
         r"routes silent", r"not a safely running")


def _banner(text: str) -> str:
    """The safety blockquote: every `>` line of the first blockquote under "Safety model" (it led
    the README until the docs pass moved the pitch and "Before you start" above it)."""
    lines = text[text.index("## Safety model"):].splitlines()[1:]
    out = []
    for line in lines:
        if line.startswith(">") or (out and not line.strip()):
            out.append(line.lstrip("> "))
        elif out:
            break
    return "\n".join(out)


def _first_run(text: str) -> str:
    start = text.index("### First run, in order")
    fence = text.index("```bash", start)
    return text[fence:text.index("```\n", fence + 7)]


class BannerMatchesFirstRun(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text()
        self.banner = _banner(self.readme)
        self.steps = _first_run(self.readme)

    def test_banner_is_found(self):
        self.assertGreater(len(self.banner), 200)

    def test_no_status_line_contradicts_the_first_run_steps(self):
        for path in (ROOT / "README.md", ROOT / "docs" / "architecture.md"):
            text = path.read_text()
            for phrase in STALE:
                self.assertIsNone(re.search(phrase, text, re.I), f"{path.name}: {phrase!r}")

    def test_the_steps_and_the_banner_name_the_same_two_switches(self):
        # The steps create the runtime file and arm the hooks …
        self.assertIn(RUNTIME, self.steps)
        self.assertRegex(self.steps, r"hermes dk arm --loop ID")
        # … and the banner says those are what make a seat turn run, and what it then does.
        self.assertIn(RUNTIME, self.banner)
        self.assertRegex(self.banner, r"`arm`")
        self.assertRegex(self.banner, r"reviewer turn posts a real GitHub\s+review")
        self.assertRegex(self.banner, r"fixer pushes stay \*\*off\*\*")

    def test_the_fix_leg_banner_names_the_command_that_opts_in(self):
        self.assertIn("hermes dk fixer-push --loop ID --enable --acknowledge-pr-race",
                      self.banner.replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
