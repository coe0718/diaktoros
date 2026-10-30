"""Duplication audit (C3): one reading of an ISO-8601 timestamp, ``util.iso_epoch``.

About ten modules parsed timestamps on their own, and they disagreed: ``util.epoch`` read only the
first 19 characters, so an offset (``+05:00``) was dropped and the time taken as UTC, while the
``fromisoformat`` copies read a stamp with no zone as the host's local time. Every stamp written
today ends in ``Z``, so neither showed; the rule now has one home. The parsers that read another
format or apply a stricter rule on purpose are named in ``DIFFERENT``.

Code shipped as source to another interpreter keeps its own parser: ``seat_model._RESOLVER`` runs
in Hermes' python with ``-c`` and cannot import the plugin. The scan reads the AST, so it does not
reach into such strings.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import os
import pathlib
import sys
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import util  # noqa: E402

NOON = 1793361600.0              # 2026-10-30T12:00:00Z
# The parsers that differ on purpose, by function: another format, or a stricter rule.
DIFFERENT = {
    "diaktoros/util.py::iso_epoch": "the helper itself",
    "diaktoros/gate.py::latest_effective_review_at_head":
        "verdict order refuses a stamp with no zone instead of reading it as UTC",
    "diaktoros/gh.py::parse_token_expiry":
        "GitHub's token-expiry header: '2026-10-01 12:00:00 UTC', not ISO-8601",
    "diaktoros/stats.py::parse_since": "a --since day ('2026-10-30'), not a timestamp",
}


class OneReading(unittest.TestCase):
    def test_every_spelling_of_one_instant_reads_the_same(self):
        for stamp in ("2026-10-30T12:00:00Z", "2026-10-30T12:00:00+00:00",
                      "2026-10-30T17:00:00+05:00", "2026-10-30T07:00:00-05:00",
                      "2026-10-30T12:00:00.000Z", "2026-10-30T12:00:00"):
            with self.subTest(stamp):
                self.assertEqual(util.iso_epoch(stamp), NOON)
                self.assertEqual(util.epoch(stamp), NOON)

    def test_a_stamp_with_no_zone_is_utc_whatever_the_host_zone(self):
        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            time.tzset()
            try:
                self.assertEqual(util.iso_epoch("2026-10-30T12:00:00"), NOON)
            finally:
                os.environ.pop("TZ", None)
        time.tzset()

    def test_nothing_or_a_bad_stamp_is_none_and_epoch_keeps_its_zero(self):
        for value in (None, "", "yesterday", "2026-13-40T99:00:00Z", 12, ["x"]):
            with self.subTest(value=value):
                self.assertIsNone(util.iso_epoch(value))
                self.assertEqual(util.epoch(value), 0.0)


class Caller(unittest.TestCase):
    """A caller that compares a stamp with a boundary (#1488 review): the retarget filter."""

    LOOP = {"reviewer_seat": "rev-bot"}

    @staticmethod
    def review(rid, submitted_at):
        return {"id": rid, "submitted_at": submitted_at, "user": {"login": "someone", "type": "User"}}

    def test_a_zoneless_review_is_read_as_utc_against_the_retarget_boundary(self):
        from diaktoros import transition
        reviews = [self.review(1, "2026-10-30T12:00:00"),           # the instant itself: not after
                   self.review(2, "2026-10-30T12:00:01"),           # one second later: kept
                   self.review(3, "2026-10-30T16:59:59+05:00")]     # 11:59:59Z: excluded
        with mock.patch.dict(os.environ, {"TZ": "America/New_York"}):
            time.tzset()
            try:
                kept = transition.current_reviews(reviews, {"old_review_ids": [], "at": NOON},
                                                  self.LOOP)
            finally:
                os.environ.pop("TZ", None)
        time.tzset()
        self.assertEqual([r["id"] for r in kept], [2])

    def test_a_missing_or_unreadable_boundary_rejects_every_review(self):
        from diaktoros import transition
        for at in (None, "noon", [NOON]):
            with self.subTest(at=at):
                self.assertEqual(transition.current_reviews(
                    [self.review(2, "2026-10-30T12:00:01Z")], {"old_review_ids": [], "at": at},
                    self.LOOP), [])


class OneHome(unittest.TestCase):
    @staticmethod
    def parsers(source: str) -> list[tuple[str, int]]:
        """Each ``fromisoformat``/``strptime`` call in ``source``, with its enclosing function."""
        found = []

        def visit(node, scope):
            for child in ast.iter_child_nodes(node):
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr in ("fromisoformat", "strptime")):
                    found.append((scope, child.lineno))
                inner = isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                visit(child, f"{scope}.{child.name}".lstrip(".") if inner else scope)

        visit(ast.parse(source), "")
        return found

    def test_no_module_parses_a_timestamp_on_its_own(self):
        for path in sorted([*(ROOT / "diaktoros").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]):
            rel = path.relative_to(ROOT).as_posix()
            with self.subTest(rel):
                self.assertEqual([f"{func}:{line}" for func, line in self.parsers(path.read_text())
                                  if f"{rel}::{func}" not in DIFFERENT], [])

    def test_every_exception_still_parses_and_has_its_reason(self):
        for key, why in DIFFERENT.items():
            rel, func = key.split("::")
            with self.subTest(key):
                self.assertTrue(why)
                self.assertIn(func, {f for f, _ in self.parsers((ROOT / rel).read_text())})

if __name__ == "__main__":
    unittest.main()
