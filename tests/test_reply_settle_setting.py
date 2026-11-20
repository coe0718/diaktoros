"""#2248: ``reply_settle_seconds`` through every path a loop setting uses (#323).

The loop file's check (default 120, 0 = off, refused when it is not a whole number of seconds
0-3600), ``init`` from the form and from its flag, ``show``, ``set``, ``apply`` from the form and
``setup``'s hand-over to ``init``; the form's own overlay and its schema default.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import config  # noqa: E402
from diaktoros.config import lists, settings_form  # noqa: E402

KEY = "reply_settle_seconds"


class Setting(unittest.TestCase):
    def test_the_value_is_checked_and_zero_is_off_not_unset(self):
        self.assertEqual(config.DEFAULTS[KEY], 120)
        for given, want in ((None, 120), ("", 120), (" ", 120), (0, 0), ("0", 0), (45, 45),
                            (" 90 ", 90), (3600, 3600)):
            with self.subTest(given=given):
                self.assertEqual(lists.check_reply_settle_seconds(given, "x"), want)
        for bad in (-1, "-5", 3601, "x", True, False, 2.5, [], {}):
            with self.subTest(bad=bad), self.assertRaisesRegex(config.ConfigError,
                                                               "reply_settle_seconds"):
                lists.check_reply_settle_seconds(bad, "x")

    def test_the_loop_file_is_checked(self):
        import test_review_after_ci as rac
        self.assertEqual(config.normalize(rac.raw())[KEY], 120)
        self.assertEqual(config.normalize(rac.raw(reply_settle_seconds=None))[KEY], 120)
        self.assertEqual(config.normalize(rac.raw(reply_settle_seconds=0))[KEY], 0)
        self.assertEqual(config.normalize(rac.raw(reply_settle_seconds=300))[KEY], 300)
        for bad in (-1, 3601, "x", True, 1.5):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.normalize(rac.raw(reply_settle_seconds=bad))

    def test_the_form_schema_and_its_overlay(self):
        spec = settings_form.SETTINGS_SCHEMA[KEY]
        self.assertEqual((spec["type"], spec["default"]), ("int", config.DEFAULTS[KEY]))
        self.assertEqual(settings_form.settings_defaults({})[KEY], 120)
        self.assertEqual(settings_form.settings_defaults({KEY: "0"})[KEY], 0)
        import test_review_after_ci as rac
        loop = rac.raw(reply_settle_seconds=300)
        self.assertEqual(settings_form.apply_settings(loop, {KEY: "0"})[KEY], "0")
        self.assertEqual(config.normalize(settings_form.apply_settings(loop, {KEY: "0"}))[KEY], 0)
        self.assertEqual(settings_form.apply_settings(loop, {KEY: ""})[KEY], 300)  # blank keeps
        text = (pathlib.Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
        self.assertIn(f"  {KEY}:\n    label: \"{spec['label']}\"\n    type: int\n    default: 120",
                      text)


class EveryPath(unittest.TestCase):
    """init (form and flag), show, set, apply and setup, with test_shadow.Cli's harness."""
    import test_shadow as _shadow
    setUpClass = classmethod(_shadow.Cli.setUpClass.__func__)
    tearDownClass = classmethod(_shadow.Cli.tearDownClass.__func__)
    setUp, cli, init = _shadow.Cli.setUp, _shadow.Cli.cli, _shadow.Cli.init

    def value(self):
        return json.loads(self.file.read_text()).get(KEY)

    def test_every_path(self):
        rc, out = self.init()
        self.assertEqual((rc, self.value()), (0, 120), out)
        self.file.unlink()
        rc, out = self.init(settings={KEY: "30"})
        self.assertEqual((rc, self.value()), (0, 30), out)
        self.file.unlink()
        rc, out = self.init("--reply-settle-seconds", "0", settings={KEY: "30"})
        self.assertEqual((rc, self.value()), (0, 0), out)
        loop_id = self.file.stem
        rc, out = self.cli("show", "--loop", loop_id)
        self.assertIn(KEY, out)
        rc, out = self.cli("set", "--loop", loop_id, "--reply-settle-seconds", "300")
        self.assertEqual((rc, self.value()), (0, 300), out)
        rc, out = self.cli("set", "--loop", loop_id, "--reply-settle-seconds", "-1")
        self.assertEqual(self.value(), 300, out)
        self.assertIn("reply_settle_seconds must be a whole number of seconds, 0-3600", out)
        rc, out = self.cli("set", "--loop", loop_id, "--reply-settle-seconds", "0")
        self.assertEqual((rc, self.value()), (0, 0), out)
        rc, out = self.cli("apply", "--loop", loop_id, settings={KEY: "45"})
        self.assertEqual((rc, self.value()), (0, 45), out)
        rc, out = self.cli("apply", "--loop", loop_id, settings={KEY: "0"})
        self.assertEqual((rc, self.value()), (0, 0), out)
        from diaktoros.cli import setup_command as cli_setup
        t = self.t
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO,
                                            "--reply-settle-seconds", "15"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, loop_id, interactive=False)
        self.assertIn("--reply-settle-seconds=15", argv)


if __name__ == "__main__":
    unittest.main()
