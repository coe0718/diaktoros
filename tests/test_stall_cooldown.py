#!/usr/bin/env python3
"""Issue #77: ``cooldown_h`` is repeat suppression per stall, not "say it once, ever".

The sweep used to stamp every stall it *saw* into the cooldown map, alerted or not, so each
15-minute sweep refreshed the key and ``now - alerts[key]`` never passed the cooldown: an
unresolved stall spoke exactly once. The harness cannot see that — ``DIAKTOROS_TEST=1`` makes
the cooldown 0 — so these run the real ``scripts/watchdog.py`` the way cron does (no
``DIAKTOROS_TEST``, the loop's own 6h cooldown) against the fixture's GitHub stub, and move
time by ageing the watchdog's own state file rather than by waiting.
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
COOLDOWN_H = 6            # the loop default; nothing below sets it to zero


def watchdog(**extra: str) -> str:
    """One sweep exactly as cron runs it: no DIAKTOROS_TEST, so the real cooldown applies."""
    env = {**{k: v for k, v in t.env().items() if k != "DIAKTOROS_TEST"}, **extra}
    proc = subprocess.run([sys.executable, str(SCRIPTS / "watchdog.py")], capture_output=True,
                          text=True, cwd=str(SCRIPTS), timeout=120, env=env)
    assert "Traceback" not in proc.stdout + proc.stderr, proc.stdout + proc.stderr
    return proc.stdout


def watch() -> dict:
    return t.load_state("watchdog.json")


def save_watch(state: dict) -> None:
    t.state_file("watchdog.json").write_text(json.dumps(state))


def age_alerts(hours: float) -> None:
    """Move the clock: every cooldown mark is now ``hours`` older than it was."""
    state = watch()
    state["alerts"] = {k: v - hours * HOUR for k, v in state.get("alerts", {}).items()}
    save_watch(state)


class StallCooldown(unittest.TestCase):
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

    def setUp(self):
        t.reset(prs={"7": t.pr(7)})
        cfg = json.loads((t.LOOPS_DIR / "widgets.json").read_text())
        self.assertEqual(cfg.get("cooldown_h", COOLDOWN_H), COOLDOWN_H)
        # Armed hours ago, and head A was first seen after arming, three hours ago: a head the
        # reviewer has had far longer than its whole turn and never judged.
        now = time.time()
        save_watch({"armed_since": now - 4 * HOUR,
                    "heads": {"7": {"sha": t.HEAD_A, "base": "main", "base_sha": "c" * 40,
                                    "observed_at": now - 3 * HOUR, "last_seen_at": now}}})

    def stalls(self, out: str) -> list[str]:
        return [line for line in out.splitlines() if "#7  reviewer never posted a verdict" in line]

    def test_a_persisting_stall_re_alerts_once_per_cooldown(self):
        self.assertEqual(len(self.stalls(watchdog())), 1)             # first sweep: said
        (key, stamp), = watch()["alerts"].items()
        self.assertTrue(key.startswith(f"7:{t.HEAD_A[:7]}:"), key)

        age_alerts(0.25)                                             # the next 15-minute sweep
        self.assertEqual(self.stalls(watchdog()), [])                 # inside the cooldown: quiet
        # A suppressed sweep must not refresh the mark — that refresh was the whole bug.
        self.assertEqual(watch()["alerts"][key], stamp - 0.25 * HOUR)

        age_alerts(COOLDOWN_H - 0.5)                                 # still inside: quiet
        self.assertEqual(self.stalls(watchdog()), [])

        age_alerts(0.5)                                              # past cooldown_h: said again
        self.assertEqual(len(self.stalls(watchdog())), 1)
        self.assertEqual(self.stalls(watchdog()), [])                 # and the clock restarted

    def test_a_cleared_stall_that_returns_alerts_at_once(self):
        self.assertEqual(len(self.stalls(watchdog())), 1)
        t.set_prs({"7": t.pr(7, draft=True)})                        # not a stall while a draft
        self.assertEqual(self.stalls(watchdog()), [])
        self.assertEqual(watch().get("alerts"), {})                  # cleared: its mark is gone
        t.set_prs({"7": t.pr(7)})                                    # back, same head, same stall
        self.assertEqual(len(self.stalls(watchdog())), 1)             # a new stall: no cooldown

    def test_the_cooldown_map_holds_only_what_is_stalled_now(self):
        state = watch()
        state["alerts"] = {"9:deadbee:reviewer never posted a ": time.time() - HOUR,
                           "queue:fixer:acme/widgets#3": time.time() - 2 * HOUR}
        save_watch(state)
        watchdog()
        self.assertEqual([k.split(":")[0] for k in watch()["alerts"]], ["7"])

    def test_a_stuck_queue_entry_re_alerts_once_per_cooldown(self):
        t.set_prs({})
        # A queue entry held far past its seat's stall grace, and never drained (no runtime).
        pending = {"reviewer": {f"{t.REPO}#7": {"head": t.HEAD_A, "at": time.time() - 5 * HOUR,
                                                "reason": "isolated worker unavailable"}}}
        t.state_file("pending.json").write_text(json.dumps(pending))

        def stuck(out: str) -> list[str]:
            return [line for line in out.splitlines() if f"reviewer queue: {t.REPO}#7" in line]

        self.assertEqual(len(stuck(watchdog())), 1)
        age_alerts(0.25)
        self.assertEqual(stuck(watchdog()), [])
        age_alerts(COOLDOWN_H)
        self.assertEqual(len(stuck(watchdog())), 1)
        t.state_file("pending.json").write_text(json.dumps({}))       # cleared
        watchdog()
        self.assertNotIn(f"queue:reviewer:{t.REPO}#7", watch().get("alerts", {}))
        t.state_file("pending.json").write_text(json.dumps(pending))  # and back: said at once
        self.assertEqual(len(stuck(watchdog())), 1)

    def test_an_unreadable_pr_keeps_its_mark_and_does_not_re_alert(self):
        self.assertEqual(len(self.stalls(watchdog())), 1)
        marks = watch()["alerts"]
        # GitHub fails the review read: the stall is unknown, not cleared.
        mode = t.TMP / "reviews_down.json"
        stub = t.TMP / "reviews_down_stub.py"
        stub.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            f"with open({str(mode)!r}) as f:\n    down = json.load(f)\n"
            "if down and sys.argv[1].split('?')[0].endswith('/reviews'):\n"
            "    print(json.dumps({'__gh_stub_response__': {'status': 502,"
            " 'body': {'message': 'stubbed'}}}))\n"
            f"else:\n    os.execv({str(t.STUB)!r}, [{str(t.STUB)!r}, *sys.argv[1:]])\n")
        stub.chmod(0o755)
        mode.write_text("true")
        watchdog(DIAKTOROS_GH_STUB=str(stub))
        self.assertEqual(watch()["alerts"], marks)
        mode.write_text("false")                                     # reads work again
        self.assertEqual(self.stalls(watchdog(DIAKTOROS_GH_STUB=str(stub))), [])


class StallKey(unittest.TestCase):
    def test_an_ageing_stall_keeps_one_key(self):
        from scripts.watchdog import stall_key
        grow = [stall_key(7, t.HEAD_A, f"adjudicating for {h:.1f}h (since x) but no run")
                for h in (1.2, 1.3, 12.0)]
        self.assertEqual(len(set(grow)), 1, grow)
        self.assertNotEqual(stall_key(7, t.HEAD_A, "reviewer never posted a verdict"),
                            stall_key(7, t.HEAD_A, "fixer never pushed — changes"))


if __name__ == "__main__":
    unittest.main()
