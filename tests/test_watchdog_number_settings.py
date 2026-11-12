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
            for bad in ("abc", "6", -1, 0, True, False, [], float("inf"), float("nan")):
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

    def test_positive_numbers_are_kept(self):
        loop = config.normalize(raw_loop(cooldown_h=0.5, marker_grace_min=90))
        self.assertEqual(loop["cooldown_h"], 0.5)
        self.assertEqual(loop["marker_grace_min"], 90)


if __name__ == "__main__":
    unittest.main()
