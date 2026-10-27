"""Every CI job has a time limit (2026-10-27: a hung unit shard on #1407 ran past 30 minutes,
toward GitHub's 6-hour default, because the `unit` job had none). Read as text: the tests are
stdlib-only, so no YAML parser."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import re
import unittest

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def jobs(text: str) -> dict[str, str]:
    """Each job under ``jobs:`` and the text of its block."""
    body = text.split("\njobs:\n", 1)[1]
    found = {}
    for block in re.split(r"\n(?=  [A-Za-z0-9_-]+:\n)", "\n" + body):
        match = re.match(r"\n?  ([A-Za-z0-9_-]+):\n", block)
        if match:
            found[match.group(1)] = block
    return found


class Timeouts(unittest.TestCase):
    def test_every_job_has_a_time_limit(self):
        found = jobs(WORKFLOW.read_text())
        self.assertGreaterEqual(len(found), 6, sorted(found))
        missing = [name for name, block in found.items()
                   if not re.search(r"^    timeout-minutes: \d+$", block, re.M)]
        self.assertEqual(missing, [], "jobs without timeout-minutes")


if __name__ == "__main__":
    unittest.main()
