"""Issue #1106: ``cooldown_h`` and ``marker_grace_min`` are validated as positive numbers when
the loop file loads, so a bad value is a ConfigError naming the key, not a ``ValueError`` in the
watchdog sweep. ``null`` still means the default (#1066)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)

import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from diaktoros import config  # noqa: E402

KEYS = ("cooldown_h", "marker_grace_min")
MINUTE_KEYS = ("grace_min", "ttl_min")   # whole minutes, read with int() (#1692)


def raw_loop(**extra) -> dict:
    return {"id": "widgets", "repo": "acme/widgets", "fixers": ["fixer"], "reviewers": ["reviewer"],
            "read_token": "reader", "host": "https://gw.example",
            "seats": {"reviewer": {"route": "widgets-review", "profile": "reviewer"},
                      "fixer": {"route": "widgets-fix", "profile": "fixer"}}, **extra}


class NumberSettingsTest(unittest.TestCase):
    def test_bad_values_are_a_config_error_naming_the_key(self):
        for key in KEYS:
            for bad in ("abc", "0", "-6", "inf", "nan", "1e400", -1, 0, True, False, [],
                        float("inf"), float("nan")):
                with self.subTest(key=key, value=bad):
                    with self.assertRaises(config.ConfigError) as ctx:
                        config.normalize(raw_loop(**{key: bad}))
                    self.assertIn(repr(key), str(ctx.exception))

    def test_out_of_range_integers_are_a_config_error_not_an_overflow(self):
        for key in KEYS:
            # 10**5000 is past Python's 4300-digit int->str limit: repr() itself raises ValueError
            for bad in (-(10 ** 400), 10 ** 400, -(10 ** 5000), 10 ** 5000, [10 ** 5000]):
                with self.subTest(key=key, kind=type(bad).__name__):
                    with self.assertRaises(config.ConfigError) as ctx:
                        config.normalize(raw_loop(**{key: bad}))
                    self.assertIn(repr(key), str(ctx.exception))
                    self.assertLess(len(str(ctx.exception)), 300)

    def test_a_bad_value_fails_when_the_loop_file_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "widgets.json"
            path.write_text(json.dumps(raw_loop(cooldown_h="abc")))
            with self.assertRaises(config.ConfigError) as ctx:
                config.load_file(path)
            self.assertIn("'cooldown_h'", str(ctx.exception))

    def test_null_and_absent_mean_the_default(self):
        for key in KEYS:
            with self.subTest(key=key):
                self.assertEqual(config.normalize(raw_loop(**{key: None}))[key], config.DEFAULTS[key])
                self.assertEqual(config.normalize(raw_loop())[key], config.DEFAULTS[key])

    def test_empty_string_means_the_default_and_the_docs_say_so(self):
        # Issue #1628: the empty string is accepted as the default, so the settings table
        # must list it next to ``null`` instead of saying all text is refused.
        docs = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "configuration.md").read_text()
        for key in KEYS:
            with self.subTest(key=key):
                self.assertEqual(config.normalize(raw_loop(**{key: ""}))[key], config.DEFAULTS[key])
                row = next(line for line in docs.splitlines() if line.startswith(f"| `{key}` |"))
                self.assertIn('the empty string `""`', row)

    def test_bad_grace_and_ttl_minutes_are_a_config_error_naming_the_key(self):
        # Issue #1692: config.stall_grace_s / seat_ttl_s do int(...) mid-sweep.
        for key in MINUTE_KEYS:
            for bad in ("abc", "4.5", -1, 0, "0", 2.5, True, [], float("inf"), float("nan")):
                with self.subTest(key=key, value=bad):
                    with self.assertRaises(config.ConfigError) as ctx:
                        config.normalize(raw_loop(**{key: bad}))
                    self.assertIn(repr(key), str(ctx.exception))

    def test_grace_and_ttl_minutes_read_as_whole_numbers(self):
        for key in MINUTE_KEYS:
            for given, kept in (("40", 40), (40, 40), (40.0, 40), (None, config.DEFAULTS[key]),
                                ("", config.DEFAULTS[key])):
                with self.subTest(key=key, value=given):
                    loop = config.normalize(raw_loop(**{key: given}))
                    self.assertEqual(loop[key], kept)
                    self.assertIs(type(loop[key]), int)

    def test_a_bad_grace_fails_at_load_not_in_the_watchdog_sweep(self):
        # The sweep only ever sees a loaded loop: a bad grace_min must stop at load_file.
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "widgets.json"
            path.write_text(json.dumps(raw_loop(grace_min="abc")))
            with self.assertRaises(config.ConfigError) as ctx:
                config.load_file(path)
            self.assertIn("'grace_min'", str(ctx.exception))

    def test_grace_and_ttl_minutes_past_the_ceiling_are_a_config_error(self):
        # Review of #2411: 10**310 passed the >= 1 check, then overflowed the sweep's float
        # division and split the seat ledger's TTL from the watchdog's dead-claim threshold.
        for key in MINUTE_KEYS:
            for bad in (10 ** 310, float(10 ** 20), 10 ** 7, "10000000"):
                with self.subTest(key=key, value=bad if isinstance(bad, str) else len(str(bad))):
                    with self.assertRaises(config.ConfigError) as ctx:
                        config.normalize(raw_loop(**{key: bad}))
                    self.assertIn(repr(key), str(ctx.exception))
        top = getattr(config, "TIMER_MINUTES_MAX", None)
        self.assertEqual(top, 525_600)      # one year of minutes, the documented ceiling
        for key in MINUTE_KEYS:
            self.assertEqual(config.normalize(raw_loop(**{key: top}))[key], top)
            with self.assertRaisesRegex(config.ConfigError, f"1-{top}"):
                config.normalize(raw_loop(**{key: top + 1}))

    def test_minutes_that_load_keep_the_sweep_and_seat_ledger_in_agreement(self):
        # The reviewer's reproduction, through the watchdog sweep's own entry point: any
        # grace_min/ttl_min that normalize accepts must be one _scan_stalls can divide, and the
        # seat ledger must read the same TTL the dead-claim threshold uses.
        from types import SimpleNamespace
        from unittest import mock
        from diaktoros import state as state_mod, watch_mode
        from scripts import watchdog
        for minutes in (10 ** 310, 525_600):
            with self.subTest(digits=len(str(minutes))), tempfile.TemporaryDirectory() as tmp:
                try:
                    loop = config.normalize(raw_loop(grace_min=minutes, ttl_min=minutes,
                                                     state_dir=str(pathlib.Path(tmp) / "st")))
                except config.ConfigError:
                    continue            # refused at load: nothing downstream ever sees it
                st = state_mod.LoopState(loop)
                sweep = SimpleNamespace(loop=loop, st=st, watch={}, prs=[])
                with mock.patch.object(watch_mode, "TEST", False):
                    watchdog._scan_stalls(sweep)
                self.assertEqual(sweep.grace["reviewer"], minutes)
                self.assertEqual(st.minutes("ttl_min"), minutes)
                self.assertEqual(2 * st._seat_ttl(), config.seat_died_after_s(loop))

    def test_numeric_strings_read_as_their_number(self):
        # Issue #1692's open decision: coerce, like _as_int does for the integer settings.
        for key in KEYS:
            for given, kept in (("6", 6), (" 6 ", 6), ("0.5", 0.5)):
                with self.subTest(key=key, value=given):
                    value = config.normalize(raw_loop(**{key: given}))[key]
                    self.assertEqual(value, kept)
                    self.assertIs(type(value), type(kept))

    def test_positive_numbers_are_kept(self):
        loop = config.normalize(raw_loop(cooldown_h=0.5, marker_grace_min=90))
        self.assertEqual(loop["cooldown_h"], 0.5)
        self.assertEqual(loop["marker_grace_min"], 90)


if __name__ == "__main__":
    unittest.main()
