"""Which snapshot docs/site/build-data.py starts from, and keeps when the API fails.

The Pages deploy refreshes data.json into its artifact only, so the checked-in file can be older
than what the site last published (Vex, PR #2570). These tests stub the script's api() and point
OUT at a temp file, so they never touch the network or the real data.json.
"""
import _home_guard  # noqa: F401  # first import: temp HOME/HERMES_HOME
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from test_site_data import load_module


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

    def test_freshness_is_the_refresh_stamp_not_the_last_push(self):
        # Vex, PR #2570 r2: a release or a star changes the figures without a
        # push, so two snapshots with one pushed_at can still differ in age.
        refreshed = dict(self.NEW, pushed_at=self.OLD["pushed_at"],
                         refreshed_at="2026-02-03T10:00:00Z")
        _, data = self.main(self.down, deployed=refreshed)
        self.assertEqual(data["version"], "v2")
        older = dict(self.OLD, refreshed_at="2026-01-02T00:00:00Z")
        _, data = self.main(self.down, checked_in=older,
                            deployed=dict(refreshed, pushed_at="2025-01-01T00:00:00Z"))
        self.assertEqual(data["version"], "v2", "a later refresh wins over a later push")

    def test_a_checked_in_snapshot_refreshed_later_is_kept(self):
        # A hand refresh committed after the last deploy is the newer one.
        newer = dict(self.OLD, refreshed_at="2026-03-01T00:00:00Z")
        for deployed in (dict(self.NEW, refreshed_at="2026-02-03T10:00:00Z"), self.NEW):
            with self.subTest(deployed=deployed):
                _, data = self.main(self.down, checked_in=newer, deployed=deployed)
                self.assertEqual(data["version"], "v1")

    def test_a_successful_refresh_stamps_its_time(self):
        def api(path):
            if "commit_activity" in path:
                return []
            if "releases/latest" in path:
                return {"tag_name": "v3", "html_url": "u", "published_at": "2026-03-03T00:00:00Z"}
            if "search/issues" in path:
                return {"total_count": 4}
            return {"stargazers_count": 5, "forks_count": 1, "open_issues_count": 6,
                    "pushed_at": "2026-03-01T00:00:00Z"}
        from datetime import datetime, timezone
        before = datetime.now(timezone.utc).replace(microsecond=0)
        _, data = self.main(api)
        stamped = datetime.fromisoformat(data["refreshed_at"].replace("Z", "+00:00"))
        self.assertLessEqual(before, stamped)
        _, failed = self.main(self.down, checked_in=data)
        self.assertEqual(failed["refreshed_at"], data["refreshed_at"], "a failure keeps the stamp")

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
