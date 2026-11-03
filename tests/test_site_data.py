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

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "docs" / "site"


def load_module():
    spec = importlib.util.spec_from_file_location("site_build_data", SITE / "build-data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class BuildDataTests(unittest.TestCase):
    def run_main(self, prev, weeks):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmpdir, True))
        out_path = pathlib.Path(tmpdir) / "data.json"
        out_path.write_text(json.dumps(prev))
        mod = load_module()
        mod.OUT = str(out_path)

        def fake_api(path):
            if "commit_activity" in path:
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
