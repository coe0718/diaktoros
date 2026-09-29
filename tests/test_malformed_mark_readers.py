#!/usr/bin/env python3
"""Issue #80: the watchdog and ``status`` skip a malformed mark instead of crashing.

#96 taught the state readers (``live_locks``/``active``/``explain``) that a mark whose ``at`` is
not a plain number is junk. Four readers still did their own arithmetic or sorting on ``at`` —
the drain's queue sort, the sweep's stuck-queue line, ``died_locks`` and ``status``'s "running:"
line — so one malformed entry in ``pending.json`` or ``locks.json`` still raised ``TypeError``
out of the whole sweep (the drain runs on every handoff, for every loop).

Each test writes one junk entry next to a well-formed one, for every audited shape, and drives
the real ``scripts/watchdog.py`` (or ``status`` through the CLI entry): nothing raises, the
well-formed entry is still drained, reported or shown, and the junk one is skipped or pruned.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)

import json
import os
import pathlib
import subprocess
import sys
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402

SCRIPTS = t.ROOT / "scripts"
HOUR = 3600.0

GOOD = f"{t.REPO}#7"
JUNK = f"{t.REPO}#8"


def junk_shapes(good: dict) -> dict[str, object]:
    """The audited malformed shapes (#96/#80), each a whole entry for the junk key."""
    return {
        "iso string at": {**good, "at": "2026-09-28T12:00:00Z"},
        "null at": {**good, "at": None},
        "bool at": {**good, "at": True},
        "list at": {**good, "at": [good["at"]]},
        "infinite at": {**good, "at": float("inf")},     # json writes it as Infinity
        "non-dict entry": ["not", "a", "mark"],
    }


def sweep() -> str:
    """One sweep as cron runs it (no REVIEW_LOOP_TEST), failing on any traceback."""
    env = {k: v for k, v in t.env().items() if k != "REVIEW_LOOP_TEST"}
    proc = subprocess.run([sys.executable, str(SCRIPTS / "watchdog.py")], capture_output=True,
                          text=True, cwd=str(SCRIPTS), timeout=120, env=env)
    assert "Traceback" not in proc.stdout + proc.stderr, proc.stdout + proc.stderr
    return proc.stdout


def write_state(name: str, data: dict) -> None:
    path = t.state_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class MalformedMarkReaders(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def armed(self, prs: dict) -> None:
        """A loop armed hours ago with #7 already observed, as the stall tests set it up."""
        t.reset(prs=prs)
        now = time.time()
        write_state("watchdog.json", {
            "armed_since": now - 4 * HOUR,
            "heads": {"7": {"sha": t.HEAD_A, "base": "main", "base_sha": "c" * 40,
                            "observed_at": now - 3 * HOUR, "last_seen_at": now}}})

    # 1. scripts/watchdog.py drain(): the queue sort.
    def test_drain_skips_a_junk_queue_entry_and_still_drains_the_good_one(self):
        for shape, junk in junk_shapes({"at": time.time(), "head": t.HEAD_A, "url": "u",
                                        "reason": "busy"}).items():
            with self.subTest(shape):
                t.reset(prs={"7": t.pr(7), "8": t.pr(8)})
                good = {"at": time.time(), "head": t.HEAD_A, "url": "u", "reason": "busy"}
                write_state("pending.json", {"reviewer": {JUNK: junk, GOOD: good}})
                before = len(t.RECEIVED)
                _, out, err = t.run("watchdog.py", None, "--loop", "widgets", "--drain",
                                    "--seat", "reviewer")
                self.assertNotIn("Traceback", out + err)
                woken = [json.loads(r["body"])["pull_request"]["number"]
                         for r in t.RECEIVED[before:]]
                self.assertEqual(woken, [7], out + err)          # the good one, and only it
                queue = t.load_state("pending.json").get("reviewer", {})
                self.assertNotIn(JUNK, queue)                    # the junk one is pruned
                self.assertIn(GOOD, queue)                       # the good one kept for its ack

    # 2. scripts/watchdog.py sweep: the stuck-queue line.
    def test_stuck_queue_line_skips_a_junk_entry_and_still_reports_the_good_one(self):
        old = time.time() - 5 * HOUR
        for shape, junk in junk_shapes({"at": old, "head": t.HEAD_A,
                                        "reason": "isolated worker unavailable"}).items():
            with self.subTest(shape):
                self.armed(prs={})
                good = {"at": old, "head": t.HEAD_A, "reason": "isolated worker unavailable"}
                write_state("pending.json", {"reviewer": {JUNK: junk, GOOD: good}})
                out = sweep()
                self.assertEqual(
                    len([line for line in out.splitlines()
                         if f"reviewer queue: {GOOD} waiting" in line]), 1, out)
                self.assertNotIn(f"reviewer queue: {JUNK}", out)
                queue = t.load_state("pending.json").get("reviewer", {})
                self.assertNotIn(JUNK, queue)                    # the sweep's drain pruned it
                self.assertIn(GOOD, queue)

    # 3. scripts/watchdog.py died_locks().
    def test_died_locks_skips_a_junk_mark_and_still_reports_the_good_one(self):
        dead = time.time() - 48 * HOUR
        for shape, junk in junk_shapes({"at": dead, "head": t.HEAD_A, "why": "working"}).items():
            with self.subTest(shape):
                self.armed(prs={})
                good = {"at": dead, "head": t.HEAD_B, "why": "working"}
                write_state("locks.json", {"reviewer": {JUNK: junk, GOOD: good}})
                out = sweep()
                self.assertEqual(
                    len([line for line in out.splitlines()
                         if f"reviewer slot held" in line and GOOD in line]), 1, out)
                self.assertNotIn(f"on {JUNK}", out)
                self.assertNotIn(JUNK, t.load_state("locks.json").get("reviewer", {}))

    # 4. review_loop/cli.py status: the "running:" line (and the queued lines under it).
    def test_status_marks_a_junk_lock_unreadable_and_still_shows_the_good_one(self):
        for shape, junk in junk_shapes({"at": time.time() - 600, "head": t.HEAD_A,
                                        "why": "working"}).items():
            with self.subTest(shape):
                t.reset(prs={})
                good = {"at": time.time() - 600, "head": t.HEAD_B, "why": "working"}
                write_state("locks.json", {"reviewer": {JUNK: junk, GOOD: good}})
                queued = {"at": time.time(), "head": t.HEAD_A, "url": "u", "reason": "busy"}
                write_state("pending.json", {"fixer": {JUNK: junk, GOOD: queued}})
                parsed = t.parser_for().parse_args(["status", "--loop", "widgets"])
                try:
                    _, out = t.run_cli(parsed)
                except Exception as exc:  # noqa: BLE001 - "must not raise" is the property
                    self.fail(f"status raised {type(exc).__name__}: {exc}")
                self.assertIn(f"running:    reviewer on {GOOD} for 10m", out)
                self.assertNotIn(f"running:    reviewer on {JUNK}", out)
                self.assertIn(f"reviewer mark for {JUNK} is unreadable", out)
                self.assertIn(f"queued:     fixer · {GOOD} — busy", out)
                self.assertIn(f"fixer · {JUNK} is unreadable", out)


if __name__ == "__main__":
    unittest.main()
