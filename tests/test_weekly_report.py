"""The weekly report through the observer (#658): schedule, one delivery per window, retry,
opt-out, per-loop isolation, and the same calculation as ``hermes dk report``.

The observer's POST is the only thing faked (``observer._post``); the observer ledger, the run
ledger and the schedule are real.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from diaktoros import config, gate, gh, observer, pr_hold, run_supervisor, state as state_mod, stats  # noqa: E402
from diaktoros.config import blocks  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402


def utc(*parts) -> float:
    return datetime(*parts, tzinfo=timezone.utc).timestamp()


WED_NOON = utc(2026, 10, 14, 12, 0)          # a Wednesday
MON_NINE = utc(2026, 10, 12, 9, 0)


class Schedule(unittest.TestCase):
    def test_the_latest_scheduled_instant_at_or_before_now(self):
        self.assertEqual(observer.weekly_due("mon 09:00", WED_NOON), MON_NINE)
        self.assertEqual(observer.weekly_due("mon 09:00", MON_NINE), MON_NINE)        # on the dot
        self.assertEqual(observer.weekly_due("mon 09:00", MON_NINE - 60), MON_NINE - 7 * 86400)
        self.assertEqual(observer.weekly_due("wed 13:00", WED_NOON), utc(2026, 10, 7, 13, 0))
        self.assertEqual(observer.weekly_due("wed 11:00", WED_NOON), utc(2026, 10, 14, 11, 0))
        self.assertIsNone(observer.weekly_due("someday", WED_NOON))

    def test_the_setting_is_a_weekday_and_a_utc_time(self):
        self.assertEqual(config.check_weekly_report("  Mon   09:00 "), "mon 09:00")
        for bad in ("monday 09:00", "mon 9:00", "mon 24:00", "mon 09:60", "", None, 9):
            with self.assertRaises(ValueError, msg=repr(bad)):
                config.check_weekly_report(bad)
        feed = blocks.normalize_observer({"route": "r", "weekly_report": "fri 17:30"})
        self.assertEqual(feed["weekly_report"], "fri 17:30")
        broken = blocks.normalize_observer({"route": "r", "weekly_report": "friday"})
        self.assertIn("observer.weekly_report", broken["misconfigured"])   # said, never silent
        self.assertNotIn("weekly_report", blocks.normalize_observer({"route": "r"}))
        self.assertIn("weekly report fri 17:30 UTC", observer.describe(feed))


class Form(unittest.TestCase):
    """The settings form (#658): the same setting, pushed by `apply`, the default for `init`."""
    RAW = {**fg.raw_loop(False), "observer": {"route": "one-observe", "profile": "p"}}

    def test_the_form_field_matches_plugin_yaml(self):
        spec = settings_form.SETTINGS_SCHEMA["observer_weekly_report"]
        text = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
        block = text.split("  observer_weekly_report:\n", 1)[1].split("\n  ", 4)
        self.assertIn(f'label: "{spec["label"]}"', block[0])
        self.assertIn(f'description: "{spec["description"]}"', "\n  ".join(block[:4]))

    def test_every_form_field_has_a_row_in_the_desktop_settings_reference(self):
        doc = (Path(__file__).resolve().parents[1] / "docs" / "settings.md").read_text()
        for key in settings_form.SETTINGS_SCHEMA:
            self.assertIn(f"| `{key}` |", doc, key)

    def test_apply_sets_clears_and_leaves_it(self):
        applied = settings_form.apply_settings(self.RAW, {"observer_weekly_report": "Tue 08:00"})
        self.assertEqual(applied["observer"]["weekly_report"], "tue 08:00")
        self.assertEqual(config.normalize(applied)["observer"]["weekly_report"], "tue 08:00")
        cleared = settings_form.apply_settings(applied, {"observer_weekly_report": "off"})
        self.assertNotIn("weekly_report", cleared["observer"])
        kept = settings_form.apply_settings(applied, {"observer_weekly_report": ""})
        self.assertEqual(kept["observer"]["weekly_report"], "tue 08:00")     # blank: not set here

    def test_show_lists_it_under_the_form_s_name(self):
        from diaktoros import show
        loop = config.normalize({**self.RAW, "observer": {**self.RAW["observer"],
                                                          "weekly_report": "tue 08:00"}})
        rows = {row.name: row for row in show._rows(loop)}
        self.assertEqual(rows["observer_weekly_report"].value, "tue 08:00 UTC")
        self.assertEqual(rows["observer_weekly_report"].path, ("observer", "weekly_report"))
        self.assertNotIn("observer.weekly_report", rows)                   # one row, not two

    def test_apply_refuses_by_name(self):
        with self.assertRaisesRegex(config.ConfigError, "observer_weekly_report: 'tuesday'"):
            settings_form.apply_settings(self.RAW, {"observer_weekly_report": "tuesday"})
        with self.assertRaisesRegex(config.ConfigError, "needs the loop's observer feed"):
            settings_form.apply_settings(fg.raw_loop(False), {"observer_weekly_report": "tue 08:00"})


class Feed(fg.Base):
    def setUp(self):
        super().setUp()
        self.db = run_supervisor.production_ledger()
        self.posts = []
        self.answer = (True, "", False)
        patcher = mock.patch.object(observer, "_post", side_effect=self.post)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.feed("one", weekly="mon 09:00")

    def post(self, loop, block, tag, delivery="", urgent=False):
        self.posts.append((loop["id"], block, delivery))
        return self.answer

    def feed(self, loop_id, **observer_cfg):
        raw = {**fg.raw_loop(False), "id": loop_id, "state_dir": str(self.root / f"state-{loop_id}"),
               "observer": {"route": f"{loop_id}-observe",
                            **({"weekly_report": observer_cfg.pop("weekly")}
                               if "weekly" in observer_cfg else {}),
                            **observer_cfg}}
        (config.config_dir() / f"{loop_id}.json").write_text(json.dumps(raw))
        loop = config.load_id(loop_id)
        if loop_id == "one":
            self.loop, self.st = loop, state_mod.state_for(loop)
        return loop

    def weekly(self, loop=None, now=WED_NOON):
        loop = loop or self.loop
        return stats.weekly(loop, state_mod.state_for(loop), self.db, now)


class Delivery(Feed):
    def test_one_report_per_window_with_the_report_command_s_calculation(self):
        with mock.patch.object(observer.routes, "delivery_id", return_value="d-1"):
            self.assertTrue(self.weekly())
        self.assertFalse(self.weekly())                         # the next sweep: already sent
        self.assertFalse(self.weekly(now=WED_NOON + 86400))     # still the same window
        self.assertEqual(len(self.posts), 1)
        _, block, delivery = self.posts[0]
        self.assertEqual((block["event"], block["url"], delivery),
                         ("report", f"https://github.com/{self.loop['repo']}", "d-1"))
        expected = stats.report_text(stats.report(self.loop, self.db, MON_NINE - 7 * 86400,
                                                  MON_NINE, breaches={}))
        self.assertEqual(block["message"], "📊 weekly report\n" + expected)
        self.assertIn("2026-10-05 09:00 UTC → 2026-10-12 09:00 UTC", block["message"])
        self.assertTrue(self.weekly(now=WED_NOON + 7 * 86400))  # the next week's window
        self.assertEqual(len(self.posts), 2)

    def test_a_sent_window_is_not_even_recomputed(self):
        self.weekly()
        with mock.patch.object(stats, "report") as report:      # no ledger or GitHub reads
            self.assertFalse(self.weekly())
        report.assert_not_called()

    def test_the_send_itself_claims_the_window_once(self):
        self.assertTrue(observer.send_report(self.loop, self.st, "w1", "text"))
        self.assertFalse(observer.send_report(self.loop, self.st, "w1", "text"))  # a racing sweep
        self.assertTrue(observer.send_report(self.loop, self.st, "w2", "text"))
        self.assertEqual(len(self.posts), 2)
        hushed = self.feed("hushed", weekly="mon 09:00", mute=True)
        self.assertFalse(observer.send_report(hushed, state_mod.state_for(hushed), "w1", "text"))
        self.assertEqual(len(self.posts), 2)

    def test_a_failed_delivery_is_retried_once_and_not_sent_again(self):
        self.answer = (False, "route 'one-observe' is missing", False)   # definitely not sent
        self.assertFalse(self.weekly())
        self.assertFalse(self.weekly())                         # claimed: the sweep won't resend
        self.assertEqual(len(self.posts), 1)
        self.answer = (True, "", False)
        self.assertEqual(observer.retry(self.loop, self.st), 1)
        self.assertEqual(self.posts[1][1]["event"], "report")
        self.assertEqual(self.posts[1][2], self.posts[0][2])    # the same logical delivery
        self.assertFalse(self.weekly())
        self.assertEqual(len(self.posts), 2)

    def test_an_unverified_post_is_left_for_the_operator(self):
        self.answer = (False, "POST result unverified", True)
        self.weekly()
        self.assertEqual(observer.retry(self.loop, self.st), 0)
        self.assertEqual(len(self.posts), 1)

    def test_off_muted_or_without_a_feed_sends_nothing(self):
        self.assertFalse(self.weekly(self.feed("plain")))
        with mock.patch.object(stats, "report") as report:      # muted: not even computed
            self.assertFalse(self.weekly(self.feed("hushed", weekly="mon 09:00", mute=True)))
        report.assert_not_called()
        raw = {**fg.raw_loop(False), "id": "bare", "state_dir": str(self.root / "state-bare")}
        (config.config_dir() / "bare.json").write_text(json.dumps(raw))
        self.assertFalse(self.weekly(config.load_id("bare")))
        self.assertEqual(self.posts, [])

    def test_the_events_filter_and_the_digest_do_not_swallow_it(self):
        loop = self.feed("narrow", weekly="mon 09:00", events=["stall"], digest_min=30)
        self.assertTrue(self.weekly(loop))
        self.assertEqual(len(self.posts), 1)

    def test_loops_are_reported_apart(self):
        other = self.feed("two", weekly="mon 09:00")
        self.assertTrue(self.weekly())
        self.assertTrue(self.weekly(other))
        self.assertEqual(sorted(loop_id for loop_id, _, _ in self.posts), ["one", "two"])

    def test_a_long_report_is_cut_with_a_pointer(self):
        with mock.patch.object(stats, "report_text", return_value="line\n" * 2000):
            self.weekly()
        message = self.posts[0][1]["message"]
        self.assertLessEqual(len(message), observer.REPORT_CHARS + 60)
        self.assertTrue(message.endswith("`hermes dk report` has it all)"))


class Sweep(Feed):
    """The watchdog sweep sends it, on its first sweep and on every later one: without the
    wiring, nothing is ever scheduled. The schedule runs on the real clock: any moment has a
    latest due window."""

    def sweep(self, times=1):
        from scripts import watchdog
        with mock.patch.object(watchdog, "TEST", True), \
             mock.patch.object(gate, "hooks_armed", return_value=True), \
             mock.patch.object(watchdog.route_intent, "heal", return_value=[]), \
             mock.patch.object(gh, "open_prs", return_value=[]), \
             mock.patch.object(pr_hold, "read", return_value=(None, "")), \
             mock.patch.object(watchdog, "retry_pending_breaches"), \
             mock.patch.object(watchdog.routes, "fire"):
            for _ in range(times):
                watchdog.sweep_loop(self.loop, self.st)

    def test_the_first_sweep_sends_the_due_report_once(self):
        self.sweep()
        self.assertEqual([block["event"] for _, block, _ in self.posts], ["report"])
        self.sweep()
        self.assertEqual(len(self.posts), 1)

    def test_a_later_sweep_sends_it(self):
        with mock.patch.object(stats, "weekly"):                # the first sweep: none yet
            self.sweep()
        self.assertEqual(self.posts, [])
        self.sweep()
        self.assertEqual([block["event"] for _, block, _ in self.posts], ["report"])


if __name__ == "__main__":
    unittest.main()
