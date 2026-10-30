"""Duplication audit (C1): which listed PRs a sweep serves is ``config.loop_pr``.

The watchdog's two PR loops, ``ci_fix.sweep`` and ``stale_approval.sweep`` each repeated the
same filter (not a draft, a reviewed author's, on the loop's base, with a number and a head). The
two sweeps had no test of it; these pin what each one reads, then hold them to the one filter.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import ci, ci_fix, gh, stale_approval, state as state_mod  # noqa: E402


def pr(number, author="fix", **changes):
    listed = {"number": number, "draft": False, "user": {"login": author}, "title": "t",
              "base": {"ref": "main", "sha": "c" * 40}, "head": {"sha": f"{number:x}" * 40}}
    return {**listed, **changes}


# Served: a fixer's PR and a review-only author's. Every other one fails one condition.
SERVED = [pr(1), pr(2, author="Owner")]
NOT_SERVED = {"not a dict": ["pr"], "draft": pr(3, draft=True), "stranger": pr(4, author="who"),
              "no author": pr(5, user=None), "other base": pr(6, base={"ref": "stacked"}),
              "no number": pr(0), "no head": pr(8, head={})}


class Sweeps(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.loop = {"id": "w", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "read_token": "rev", "reviewers": ["rev"], "reviewer_seat": "rev",
                     "fixers": ["fix"], "review_only": ["owner"], "required_checks": ["tests"],
                     "state_dir": str(pathlib.Path(temp.name) / "state"), "cooldown_h": 6,
                     "ttl_min": 45, "inflight_ttl_min": 10,
                     "seats": {"reviewer": {"route": "w-review"}, "fixer": {"route": "w-fix"}}}
        self.st = state_mod.state_for(self.loop)
        self.prs = [*SERVED, *NOT_SERVED.values()]

    def test_the_ci_sweep_reads_ci_only_for_the_loops_prs(self):
        heads = []
        with mock.patch.object(ci, "read", side_effect=lambda loop, head: heads.append(head)):
            ci_fix.sweep(self.loop, self.st, self.prs)
        self.assertEqual(heads, [p["head"]["sha"] for p in SERVED])

    def test_the_stale_approval_sweep_reads_reviews_only_for_the_loops_prs(self):
        numbers = []
        with mock.patch.object(gh, "reviews", side_effect=lambda loop, number, errors=None:
                               numbers.append(number)):
            stale_approval.sweep(self.loop, self.st, self.prs, {})
        self.assertEqual(numbers, [p["number"] for p in SERVED])

    def test_each_sweep_takes_the_filter_from_config_loop_pr(self):
        # One filter, as the watchdog's two PR loops already use (#1442): a sweep that spells it
        # again can drift from the others.
        for rel in ("diaktoros/ci_fix.py", "diaktoros/stale_approval.py"):
            tree = ast.parse((ROOT / rel).read_text())
            sweep = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "sweep")
            called = {ast.unparse(n.func) for n in ast.walk(sweep) if isinstance(n, ast.Call)}
            with self.subTest(rel):
                self.assertIn("config.loop_pr", called)
                self.assertNotIn("config.reviewed_authors", called)


if __name__ == "__main__":
    unittest.main()
