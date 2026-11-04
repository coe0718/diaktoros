"""#1074: a completed job's log tail and a commit's test files are kept on disk, not re-read.

The log of a completed check run and a commit's file list never change, so the CI-fix prompt
(``ci_fix.section``, reached from the fixer's and the CI-fix turn's prompts) and the reviewer's
``fix_history`` read each once and keep it under the loop's ``state_dir``. A job still running is
never kept. The prompt is driven through ``turn_prompts.ci_sections``, the entry the fixer's and
reviewer's turns use, so removing the cache call there turns a test red.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import ci, ci_cache, ci_fix, gh  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402

RED = ci.CIState(failed=["tests"], ids={"tests": 41}, urls={"tests": "https://example.invalid/41"})
LOG = "\n".join(f"line {i}" for i in range(100)) + "\nFAIL: test_walk"


def job(status="completed"):
    return {"status": status, "steps": [{"name": "Run tests", "conclusion": "failure"}]}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.loop = {"id": "w", "repo": "acme/w", "read_token": "read", "state_dir": tmp.name,
                     "base": "main", "cap": 3, "required_checks": [], "fixers": ["fix"]}
        self.state = Path(tmp.name)
        ci_fix._TESTS_SEEN.clear()
        self.addCleanup(ci_fix._TESTS_SEEN.clear)


class JobLogs(Base):
    def fixer_prompt(self, reply, text=LOG):
        row = {"seat": "fixer", "repo": "acme/w", "pr": 7, "head": "a" * 40}
        with mock.patch.object(ci, "read", return_value=RED), \
                mock.patch.object(gh, "api", return_value=reply) as api, \
                mock.patch.object(gh, "read_text", return_value=text) as read:
            out = turn_prompts.ci_sections(self.loop, row, "fixer")
        return out, api.call_count, read.call_count

    def test_a_completed_jobs_log_is_fetched_once_through_the_fixer_prompt(self):
        first, api1, read1 = self.fixer_prompt(job())
        second, api2, read2 = self.fixer_prompt(None, text=None)   # GitHub would now fail
        self.assertEqual((api1, read1), (1, 1))
        self.assertEqual((api2, read2), (0, 0))                    # no call at all
        self.assertEqual(first, second)
        self.assertIn("Failing step: 'Run tests'", second)
        self.assertIn("| FAIL: test_walk", second)

    def test_a_running_jobs_log_is_not_kept(self):
        self.fixer_prompt(job("in_progress"))
        _, api, read = self.fixer_prompt(job("in_progress"))
        self.assertEqual((api, read), (1, 1))
        self.assertEqual(list(self.state.rglob("*")), [])

    def test_unproven_or_empty_reads_are_not_kept(self):
        for reply, text in ((None, LOG), (job(), ""), (job(), None)):
            with self.subTest(reply=reply, text=text):
                self.fixer_prompt(reply, text=text)
                self.assertFalse((self.state / ci_cache.DIRNAME).exists())

    def test_a_cache_is_per_job_and_per_repo(self):
        self.fixer_prompt(job())
        other = {**self.loop, "repo": "acme/other"}
        with mock.patch.object(gh, "api", return_value=job()) as api, \
                mock.patch.object(gh, "read_text", return_value=LOG):
            ci_fix.section(other, RED, ["tests"])
            ci_fix.section(self.loop, ci.CIState(failed=["tests"], ids={"tests": 42}), ["tests"])
        self.assertEqual(api.call_count, 2)

    def test_a_corrupt_entry_is_a_miss(self):
        self.fixer_prompt(job())
        for path in (self.state / ci_cache.DIRNAME).iterdir():
            path.write_text("not json")
        _, api, read = self.fixer_prompt(job())
        self.assertEqual((api, read), (1, 1))

    def test_a_deeply_nested_entry_is_a_miss(self):
        self.fixer_prompt(job())
        for path in (self.state / ci_cache.DIRNAME).iterdir():
            path.write_text("[" * 1100 + "0" + "]" * 1100)   # json.loads raises RecursionError
        out, api, read = self.fixer_prompt(job())
        self.assertEqual((api, read), (1, 1))
        self.assertIn("| FAIL: test_walk", out)

    def test_a_missing_state_dir_is_never_created(self):
        gone = self.state / "gone"
        self.loop["state_dir"] = str(gone)
        _, api, _ = self.fixer_prompt(job())
        self.assertEqual(api, 1)
        self.assertFalse(gone.exists())


class CommitFiles(Base):
    def read(self, detail):
        rows = [{"head": "a" * 40, "state": "succeeded", "push_confirmed": 1.0}]
        commits = [{"sha": "c" * 40, "parents": [{"sha": "a" * 40}]}]
        with mock.patch.object(ci_fix, "rows", return_value=rows), \
                mock.patch.object(gh, "fetch", return_value=(commits, "")), \
                mock.patch.object(gh, "api", return_value=detail) as api:
            return ci_fix.fix_history(self.loop, 7), api.call_count

    def test_a_commits_files_survive_the_process(self):
        detail = {"files": [{"filename": "tests/test_x.py", "status": "modified",
                             "additions": 1, "deletions": 9}]}
        first, calls = self.read(detail)
        self.assertEqual(calls, 1)
        ci_fix._TESTS_SEEN.clear()                 # a new process: only the disk is left
        second, calls = self.read(None)
        self.assertEqual(calls, 0)
        self.assertEqual(first, second)
        self.assertIn("tests/test_x.py (modified, +1 -9)", second)

    def test_a_deeply_nested_commit_entry_is_a_miss(self):
        detail = {"files": [{"filename": "tests/test_x.py", "status": "modified",
                             "additions": 1, "deletions": 9}]}
        first, _ = self.read(detail)
        ci_fix._TESTS_SEEN.clear()
        for path in (self.state / ci_cache.DIRNAME).iterdir():
            path.write_text("[" * 1100 + "0" + "]" * 1100)
        second, calls = self.read(detail)
        self.assertEqual(calls, 1)
        self.assertEqual(first, second)

    def test_an_unreadable_commit_is_not_kept(self):
        self.read(None)
        _, calls = self.read({"files": []})
        self.assertEqual(calls, 1)


if __name__ == "__main__":
    unittest.main()
