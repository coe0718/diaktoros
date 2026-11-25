"""Every CI job has a time limit (2026-10-27: a hung unit shard on #1407 ran past 30 minutes,
toward GitHub's 6-hour default, because the `unit` job had none), in every workflow, not only
ci.yml (#1447). Read as text: the tests are stdlib-only, so no YAML parser."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
# The guard lives in tools/workflow_jobs.py, outside tests/, so the host's base-failure proof sees
# a change to it (review of #2700, F2), as test_run_supervisor_modules does for its shadow rules.
sys.path.insert(0, str(ROOT / "tools"))
import workflow_jobs  # noqa: E402

jobs, missing = workflow_jobs.jobs, workflow_jobs.missing


class Timeouts(unittest.TestCase):
    def test_every_job_has_a_time_limit(self):
        text = WORKFLOW.read_text()
        self.assertGreaterEqual(len(jobs(text)), 6, sorted(jobs(text)))
        self.assertEqual(missing(text), [], "jobs without timeout-minutes")

    def test_every_workflow_job_has_a_time_limit(self):
        # #1447: deploy-site.yml and prune-runs.yml had jobs with no limit, and only ci.yml was read.
        workflows = sorted(WORKFLOW.parent.glob("*.y*ml"))
        self.assertGreater(len(workflows), 1, workflows)
        self.assertEqual(workflow_jobs.problems(workflows), [])

    def test_the_guard_never_passes_a_workflow_it_cannot_read(self):
        # Reviews of #2700: a commented `jobs:` heading (F1) and an indented root mapping (F3) hid
        # an unbounded job from the guard, as would flow style or a quoted or anchored key. Each is
        # written beside copies of the real workflows and run through the guard itself.
        unbounded = {
            "commented.yml": "on: push\njobs: # every job below\n  runaway:\n    runs-on: x\n",
            "indented.yml": " on: push\n jobs:\n   runaway:\n     runs-on: x\n",
            "flow.yml": "on: push\njobs: {runaway: {runs-on: x}}\n",
            "quoted.yml": 'on: push\n"jobs":\n  runaway:\n    runs-on: x\n',
            "anchored.yml": "on: push\njobs: &all\n  runaway:\n    runs-on: x\n",
        }
        bounded = "on: push\njobs: # every job below\n  fine:\n    runs-on: x\n    timeout-minutes: 5\n"
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
            folder = pathlib.Path(temp)
            for path in WORKFLOW.parent.glob("*.y*ml"):
                shutil.copy(path, folder / path.name)
            (folder / "bounded.yml").write_text(bounded)
            self.assertEqual(workflow_jobs.problems(folder.glob("*.yml")), [], "the control passes")
            for name, text in unbounded.items():
                with self.subTest(workflow=name):
                    (folder / name).write_text(text)
                    found = workflow_jobs.problems(folder.glob("*.yml"))
                    (folder / name).unlink()
                    self.assertTrue(any(line.startswith(f"{name}:") for line in found), found)

    def test_a_job_the_check_must_not_miss(self):
        # #1415 review F1: a header with an inline comment is a job too, never part of the one
        # before it; and a step's own timeout-minutes is not the job's.
        text = ("on: push\njobs:\n  guarded:\n    runs-on: x\n    timeout-minutes: 5\n"
                "  runaway: # added later\n    runs-on: x\n"
                "  stepped:\n    runs-on: x\n    steps:\n      - run: y\n        timeout-minutes: 3\n"
                "  quoted:   \n    runs-on: x\n    timeout-minutes: 9  # generous\n")
        self.assertEqual(sorted(jobs(text)), ["guarded", "quoted", "runaway", "stepped"])
        self.assertEqual(missing(text), ["runaway", "stepped"])


class RealWorkflow(unittest.TestCase):
    def test_a_quoted_or_unrecognised_job_is_never_missed(self):
        # #1415 review F3: a quoted job key is a job; anything else at job level fails closed.
        base = WORKFLOW.read_text().rstrip("\n")
        for added, name in (('  "runaway":\n    runs-on: x\n', "runaway"),
                            ("  'runaway': # later\n    runs-on: x\n", "runaway"),
                            ("  ? runaway\n  : runs-on: x\n", "(unrecognised: ? runaway)")):
            with self.subTest(added=added):
                self.assertIn(name, missing(base + "\n" + added))
        bounded = base + '\n  "quoted":\n    runs-on: x\n    timeout-minutes: 5\n'
        self.assertEqual(missing(bounded), [])

    def test_a_job_added_after_a_comment_is_still_checked(self):
        # #1415 review F2: an unindented comment inside ``jobs:`` is not a top-level key.
        text = WORKFLOW.read_text().rstrip("\n") + ("\n# added job below\n  runaway:\n"
                                                    "    runs-on: ubuntu-24.04\n")
        self.assertEqual(missing(text), ["runaway"])
        self.assertEqual(missing(WORKFLOW.read_text()), [])


if __name__ == "__main__":
    unittest.main()
