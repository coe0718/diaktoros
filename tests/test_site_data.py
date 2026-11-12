"""Executable tests for docs/site/build-data.py's snapshot refresh.

The page's snapshot fallback is only as honest as this script: when the
commit-activity endpoint is still computing it returns an empty list, and
an unconditional sum would overwrite a real count with zero (Vex, PR
#1750 round 1). These tests stub the script's api() and point OUT at a
temp file, so they never touch the network or the real data.json.
"""
import _home_guard  # noqa: F401  # first import: temp HOME/HERMES_HOME
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "docs" / "site"


def load_module():
    spec = importlib.util.spec_from_file_location("site_build_data", SITE / "build-data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BuildDataTests(unittest.TestCase):
    def run_main(self, prev, weeks, activity=None):
        """Run main() against stubbed API answers.

        ``activity``, when given, is called with the 1-based attempt
        number for each commit_activity read and returns (or raises)
        that attempt's answer; otherwise every read returns ``weeks``.
        """
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, True))
        out_path = pathlib.Path(tmpdir) / "data.json"
        out_path.write_text(json.dumps(prev))
        mod = load_module()
        mod.OUT = str(out_path)
        self.sleeps = []
        self.activity_calls = 0
        # Patch sleep on the real time module, never the script's own
        # ``time`` binding: a retry then goes through the script's
        # ``import time``, so dropping that import turns these tests red
        # (Vex, PR #2431 round 1).

        def fake_api(path):
            if "commit_activity" in path:
                self.activity_calls += 1
                if activity is not None:
                    return activity(self.activity_calls)
                return weeks
            if "releases/latest" in path:
                return {"tag_name": "v9.9.9-test",
                        "html_url": "https://example.test/v9.9.9",
                        "published_at": "2026-02-03T00:00:00Z"}
            if "search/issues" in path:
                return {"total_count": 42}
            return {"stargazers_count": 11, "forks_count": 3,
                    "open_issues_count": 50, "pushed_at": "2026-02-01T00:00:00Z"}

        mod.api = fake_api
        with mock.patch("time.sleep", side_effect=self.sleeps.append):
            mod.main()
        return json.loads(out_path.read_text())

    def test_empty_activity_keeps_previous(self):
        # The endpoint returns [] while it computes; the old
        # unconditional sum wrote 0 over the real count.
        data = self.run_main({"commits_year": 1368}, [])
        self.assertEqual(data["commits_year"], 1368)

    def test_empty_activity_keeps_snapshot_date(self):
        # Retaining the count while stamping today would label it as
        # part of a snapshot it predates (Vex, PR #1750 round 2).
        data = self.run_main({"commits_year": 1368, "generated": "2020-01-01"}, [])
        self.assertEqual(data["commits_year"], 1368)
        self.assertEqual(data["generated"], "2020-01-01")

    def test_empty_activity_without_earlier_date_stays_undated(self):
        # Vex round 3: the exception that stamped a retained count with
        # today when no earlier date existed claimed an age nobody
        # knows. Unknown provenance stays unknown — the page reads a
        # missing date as "last checked-in values", never as today.
        data = self.run_main({"commits_year": 1368}, [])
        self.assertEqual(data["commits_year"], 1368)
        self.assertNotIn("generated", data)

    def test_computing_activity_is_retried_and_advances_date(self):
        # #1773: the endpoint answers [] while it computes. One empty
        # read must not freeze the snapshot date while every other
        # figure is refreshed: the script asks again before retaining.
        def activity(call):
            return [] if call < 3 else [{"total": 5}, {"total": 7}]
        data = self.run_main({"commits_year": 1368, "generated": "2020-01-01"},
                             None, activity)
        self.assertEqual(data["commits_year"], 12)
        self.assertNotEqual(data["generated"], "2020-01-01")
        self.assertEqual(self.activity_calls, 3)
        self.assertEqual(len(self.sleeps), 2)

    def test_activity_error_then_data_is_retried(self):
        def activity(call):
            if call == 1:
                raise OSError("transient")
            return [{"total": 1}]
        data = self.run_main({"commits_year": 1368, "generated": "2020-01-01"},
                             None, activity)
        self.assertEqual(data["commits_year"], 1)
        self.assertNotEqual(data["generated"], "2020-01-01")

    def test_retry_waits_through_module_time_binding(self):
        # Vex, PR #2431 round 1: called directly (main() would swallow a
        # NameError), a retry must reach time.sleep through the script's
        # own import, not a binding the test created.
        mod = load_module()
        answers = iter([[], [{"total": 2}]])
        mod.api = lambda path: next(answers)
        with mock.patch("time.sleep") as sleep:
            self.assertEqual(mod.commit_activity(), [{"total": 2}])
        sleep.assert_called_once_with(mod.ACTIVITY_WAIT)

    def test_retries_are_bounded(self):
        # Empty on every attempt: the count and its date are retained
        # after ACTIVITY_ATTEMPTS reads, not polled forever.
        data = self.run_main({"commits_year": 1368, "generated": "2020-01-01"}, [])
        self.assertEqual(data["generated"], "2020-01-01")
        self.assertEqual(self.activity_calls, load_module().ACTIVITY_ATTEMPTS)

    def test_populated_activity_updates(self):
        data = self.run_main({"commits_year": 1368},
                             [{"total": 10}, {"total": 20}])
        self.assertEqual(data["commits_year"], 30)
        # A real refresh still advances the stamp.
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone.utc).date()
        self.assertIn(data["generated"],
                      {today.isoformat(), (today - timedelta(days=1)).isoformat()})


if __name__ == "__main__":
    unittest.main()
