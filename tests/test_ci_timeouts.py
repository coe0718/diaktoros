"""Every CI job has a time limit (2026-10-27: a hung unit shard on #1407 ran past 30 minutes,
toward GitHub's 6-hour default, because the `unit` job had none). Read as text: the tests are
stdlib-only, so no YAML parser."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import re
import unittest

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"
# A job's header under ``jobs:``: two spaces, its id, a colon, and at most a comment after it.
HEADER = re.compile(r"^  ([A-Za-z0-9_-]+):\s*(?:#.*)?$")
TIMEOUT = re.compile(r"^    timeout-minutes:\s*\d+\s*(?:#.*)?$")


def jobs(text: str) -> dict[str, list[str]]:
    """Each job under ``jobs:`` and its lines, read line by line."""
    found: dict[str, list[str]] = {}
    current = None
    inside = False
    for line in text.splitlines():
        if not line.startswith(" ") and line.strip():
            inside, current = line.rstrip() == "jobs:", None    # a top-level key ends the jobs
            continue
        match = HEADER.match(line) if inside else None
        if match:
            current = match.group(1)
            found[current] = []
        elif current is not None:
            found[current].append(line)
    return found


def missing(text: str) -> list[str]:
    return sorted(name for name, lines in jobs(text).items()
                  if not any(TIMEOUT.match(line) for line in lines))


class Timeouts(unittest.TestCase):
    def test_every_job_has_a_time_limit(self):
        text = WORKFLOW.read_text()
        self.assertGreaterEqual(len(jobs(text)), 6, sorted(jobs(text)))
        self.assertEqual(missing(text), [], "jobs without timeout-minutes")

    def test_a_job_the_check_must_not_miss(self):
        # #1415 review F1: a header with an inline comment is a job too, never part of the one
        # before it; and a step's own timeout-minutes is not the job's.
        text = ("on: push\njobs:\n  guarded:\n    runs-on: x\n    timeout-minutes: 5\n"
                "  runaway: # added later\n    runs-on: x\n"
                "  stepped:\n    runs-on: x\n    steps:\n      - run: y\n        timeout-minutes: 3\n"
                "  quoted:   \n    runs-on: x\n    timeout-minutes: 9  # generous\n")
        self.assertEqual(sorted(jobs(text)), ["guarded", "quoted", "runaway", "stepped"])
        self.assertEqual(missing(text), ["runaway", "stepped"])


if __name__ == "__main__":
    unittest.main()
