"""#654: one ``ready_set`` observer notice when the ready-to-merge set changes.

Driven through the watchdog sweep (the enforcing entry point): the first set is recorded silently,
a changed set is one notice, an unchanged set is none, a set that becomes empty says so, and
``hermes dk ready`` itself (``cmd_ready``) never notifies.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)

import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import observer, ready, state as state_mod  # noqa: E402
from diaktoros import watch_mode  # noqa: E402
from scripts import watchdog  # noqa: E402

NOW = time.time()


def entry(n):
    return {"number": n, "title": "t", "after": [], "note": ""}


class ReadyNotice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.loop = {"id": "w", "repo": "acme/widgets", "base": "main", "cap": 3,
                     "fixers": ["fix"], "review_only": [], "required_checks": ["tests"],
                     "observer": {"route": "r", "deliver": "telegram", "profile": "p"},
                     "state_dir": str(pathlib.Path(self.tmp.name) / "state"), "cooldown_h": 6,
                     "ttl_min": 45, "inflight_ttl_min": 10,
                     "seats": {"reviewer": {"route": "w-review"}, "fixer": {"route": "w-fix"}}}
        self.st = state_mod.state_for(self.loop)
        watch = self.st.watch()
        watch["armed_since"] = NOW - 86400
        self.st.watch_save(watch)
        self.notices: list = []

    def sweep(self, numbers, loop=None):
        loop = loop or self.loop
        with mock.patch.object(watch_mode, "TEST", True), \
             mock.patch.object(watchdog.gate, "hooks_read", return_value=(True, "")), \
             mock.patch.object(watchdog.gh, "open_prs", return_value=[]), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(watchdog.gate_shims, "heal", return_value=[]), \
             mock.patch.object(watchdog.gate_shims, "heal_watchdog_shim", return_value=[]), \
             mock.patch.object(watchdog.gate, "resume_isolated"), \
             mock.patch.object(watchdog.ci_fix, "sweep"), \
             mock.patch.object(watchdog.main_check, "sweep"), \
             mock.patch.object(watchdog.observer, "retry", return_value=0), \
             mock.patch.object(watchdog.observer, "flush"), \
             mock.patch.object(ready, "collect",
                               return_value=([entry(n) for n in numbers], [], "")), \
             mock.patch.object(watchdog.observer, "notify",
                               side_effect=lambda *a, **kw: self.notices.append((a, kw))):
            watchdog.sweep_loop(loop, self.st)
        return [n for n in self.notices if n[0][2] == "ready_set"]

    def test_the_sweep_refreshes_the_merge_check(self):
        # #1163 M2b: the backstop that posts the check whatever event hook was missed.
        with mock.patch.object(watchdog.merge_check, "refresh") as refresh:
            self.sweep([5])
        refresh.assert_called_once()
        self.assertIs(refresh.call_args.args[0], self.loop)
        with mock.patch.object(watchdog.merge_check, "refresh", side_effect=RuntimeError("x")):
            self.sweep([5])                          # a failure never stops the sweep

    def test_the_sweep_prunes_pending_escalations_to_the_open_prs(self):
        # #1246: a closed PR's pending escalation (or a lapsed one) is dropped by the sweep.
        with mock.patch.object(watchdog.escalate_pending, "prune", return_value=[]) as prune:
            self.sweep([5])
        prune.assert_called_once()
        self.assertIs(prune.call_args.args[0], self.st)

    def test_first_set_is_recorded_silently(self):
        self.assertEqual(self.sweep([5, 7]), [])

    def test_change_is_one_notice_and_same_set_is_none(self):
        self.sweep([5])
        got = self.sweep([5, 7])
        self.assertEqual(len(got), 1)
        self.assertIn("#5, #7", got[0][1]["outcome"])
        self.assertEqual(len(self.sweep([5, 7])), 1)        # still just the one

    def test_becoming_empty_is_a_change(self):
        self.sweep([5])
        got = self.sweep([])
        self.assertEqual(len(got), 1)
        self.assertIn("nothing is ready", got[0][1]["outcome"])

    def test_not_subscribed_reads_nothing(self):
        loop = {**self.loop, "observer": {**self.loop["observer"], "events": ["opened"]}}
        self.sweep([5], loop)
        self.assertEqual(self.sweep([5, 7], loop), [])
        self.assertFalse((self.st.dir / ready.FILE).exists())

    def test_event_is_routine_and_has_wording(self):
        self.assertIn("ready_set", observer.EVENTS)
        self.assertFalse(observer.is_urgent("ready_set"))
        self.assertIn("ready_set", observer.LABEL)
        self.assertIn("ready_set", observer.EMOJI)

    def test_cmd_ready_never_notifies(self):
        from diaktoros import cli
        args = mock.Mock(loop="w")
        with mock.patch.object(cli.config, "load_id", return_value=self.loop), \
             mock.patch.object(ready, "collect", return_value=([entry(5)], [], "")), \
             mock.patch.object(observer, "notify") as notify:
            cli.cmd_ready(args)
        notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
