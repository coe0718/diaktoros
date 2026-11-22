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


class SnapshotBaseTests(unittest.TestCase):
    """Vex, PR #2570 round 1: the deploy refreshes into its artifact only, so
    a failed refresh must keep the newest published snapshot, and a read
    that fails half-way must not leave new figures beside old ones."""
    OLD = {"version": "v1", "stars": 1, "prs_merged": 2,
           "pushed_at": "2026-01-01T00:00:00Z"}
    NEW = {"version": "v2", "stars": 99, "prs_merged": 7,
           "pushed_at": "2026-02-01T00:00:00Z"}

    def run(self, result=None):
        with tempfile.TemporaryDirectory() as tmp:
            self.tmp = pathlib.Path(tmp)
            return super().run(result)

    def main(self, api, checked_in=OLD, deployed=None, argv=None):
        out = self.tmp / "data.json"
        out.write_text(json.dumps(checked_in))
        if argv is None:
            argv = []
            if deployed is not None:
                (self.tmp / "deployed.json").write_text(
                    deployed if isinstance(deployed, str) else json.dumps(deployed))
                argv = ["--previous", str(self.tmp / "deployed.json")]
        mod = load_module()
        mod.OUT = str(out)
        mod.api = api
        with mock.patch("time.sleep"), mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = mod.main(argv)
        return code, json.loads(out.read_text())

    @staticmethod
    def down(path):
        raise OSError("API down")

    def test_a_failure_after_earlier_reads_keeps_every_previous_figure(self):
        def api(path):
            if "releases/latest" in path:
                return {"tag_name": "v2", "html_url": "u", "published_at": "2026-02-02T00:00:00Z"}
            if "search/issues" in path:
                raise OSError("rate limited")
            return {"stargazers_count": 99, "forks_count": 3, "open_issues_count": 5,
                    "pushed_at": "2026-02-01T00:00:00Z"}
        _, data = self.main(api)
        for key, value in self.OLD.items():
            self.assertEqual(data[key], value, key)
        self.assertIn("rate limited", data["error"])

    def test_an_outage_keeps_the_newer_published_snapshot(self):
        _, data = self.main(self.down, deployed=self.NEW)
        self.assertEqual((data["version"], data["stars"]), ("v2", 99))

    def test_an_older_or_unreadable_published_snapshot_is_ignored(self):
        older = dict(self.NEW, pushed_at="2025-12-01T00:00:00Z")
        for deployed in (older, "[1, 2]", "not json", dict(self.NEW, pushed_at=None)):
            with self.subTest(deployed=deployed):
                _, data = self.main(self.down, deployed=deployed)
                self.assertEqual(data["version"], "v1")
        _, data = self.main(self.down, argv=["--previous", str(self.tmp / "missing.json")])
        self.assertEqual(data["version"], "v1")

    def test_the_newest_snapshot_is_on_disk_before_any_read(self):
        # The deploy bounds the step: a refresh killed mid-read still
        # publishes the newest snapshot, not the checked-in one.
        def killed(path):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.main(killed, deployed=self.NEW)
        self.assertEqual(json.loads((self.tmp / "data.json").read_text())["version"], "v2")

    def test_a_bad_argument_is_refused(self):
        code, data = self.main(self.down, argv=["--previos", "x"])
        self.assertEqual((code, data), (2, self.OLD))


if __name__ == "__main__":
    unittest.main()
