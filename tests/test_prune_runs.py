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


if __name__ == "__main__":
    unittest.main()
