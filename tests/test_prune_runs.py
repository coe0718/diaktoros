"""The workflow-run pruner (#902) keeps the newest runs and every run the loop still reads."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / ".github" / "scripts" / "prune_runs.py"
spec = importlib.util.spec_from_file_location("prune_runs", SCRIPT)
prune_runs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune_runs)


def run(i, head="x" * 40, status="completed"):
    return {"id": i, "created_at": f"2026-10-{i:02d}T00:00:00Z", "head_sha": head,
            "status": status}


class Plan(unittest.TestCase):
    def test_keeps_the_newest_and_deletes_the_rest_oldest_last(self):
        runs = [run(i) for i in range(1, 11)]
        doomed = prune_runs.plan(runs, set(), keep=3)
        self.assertEqual([r["id"] for r in doomed], [7, 6, 5, 4, 3, 2, 1])

    def test_never_an_open_prs_head_or_main_whatever_its_age(self):
        runs = [run(1, head="open-pr-head"), run(2, head="main-head"), run(3), run(4), run(5)]
        doomed = prune_runs.plan(runs, {"open-pr-head", "main-head"}, keep=1)
        self.assertEqual([r["id"] for r in doomed], [4, 3])

    def test_never_an_unfinished_run(self):
        runs = [run(1, status="in_progress"), run(2, status="queued"), run(3), run(4)]
        self.assertEqual([r["id"] for r in prune_runs.plan(runs, set(), keep=1)], [3])

    def test_keep_zero_and_a_negative_keep(self):
        self.assertEqual(len(prune_runs.plan([run(1), run(2)], set(), keep=0)), 2)
        with self.assertRaises(ValueError):
            prune_runs.plan([], set(), keep=-1)


class Main(unittest.TestCase):
    """The script end to end with GitHub faked (review on #903): a dry run deletes nothing."""

    def api(self, runs, rate_limit_after=None):
        import urllib.error
        calls = []

        def call(method, path, token):
            calls.append((method, path))
            if method == "DELETE":
                if rate_limit_after is not None and sum(m == "DELETE" for m, _ in calls) > rate_limit_after:
                    raise urllib.error.HTTPError(path, 403, "rate limited", {}, None)
                return None
            if path.startswith("/repos/o/r/pulls"):
                return [{"head": {"sha": "open-pr-head"}}]
            if path == "/repos/o/r":
                return {"default_branch": "main"}
            if path.startswith("/repos/o/r/branches/"):
                return {"commit": {"sha": "main-head"}}
            if path.startswith("/repos/o/r/actions/runs"):
                return {"workflow_runs": runs if "page=1" in path else []}
            raise AssertionError(path)
        return calls, call

    def main(self, argv, runs, **kwargs):
        from unittest import mock
        calls, call = self.api(runs, **kwargs)
        with mock.patch.object(prune_runs, "_call", side_effect=call), \
                mock.patch.dict("os.environ", {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t"}):
            rc = prune_runs.main(argv)
        return rc, [path for method, path in calls if method == "DELETE"]

    def test_a_dry_run_makes_no_delete_call(self):
        runs = [run(i) for i in range(1, 11)] + [run(11, head="open-pr-head")]
        rc, deleted = self.main(["--keep", "2", "--dry-run"], runs)
        self.assertEqual((rc, deleted), (0, []))

    def test_a_real_run_deletes_the_oldest_first_within_the_cap_and_spares_protected_heads(self):
        runs = [run(i) for i in range(1, 11)] + [run(0 + 1, head="main-head") | {"id": 99}]
        rc, deleted = self.main(["--keep", "2", "--max-delete", "3"], runs)
        self.assertEqual(rc, 0)
        self.assertEqual(deleted, ["/repos/o/r/actions/runs/1", "/repos/o/r/actions/runs/2",
                                   "/repos/o/r/actions/runs/3"])
        self.assertNotIn("/repos/o/r/actions/runs/99", deleted)

    def test_a_rate_limit_stops_cleanly(self):
        rc, deleted = self.main(["--keep", "0"], [run(i) for i in range(1, 6)], rate_limit_after=2)
        self.assertEqual(rc, 0)
        self.assertEqual(len(deleted), 3)        # two deleted, the third answered 403: stop


if __name__ == "__main__":
    unittest.main()
