"""#2305: every settings-schema key reaches the loop file through `init` and `set` (#323).

``test_apply_every_form_key`` walks every ``SETTINGS_SCHEMA`` key through ``apply``. This walks
the same keys through ``hermes dk init --<flag>`` and ``hermes dk set --loop ID --<flag>``, with
the same test home, parser and values (``PUSHES``), and asserts the loop file then holds what the
form would have written: the key's ``show`` row after the command equals its row after
``settings_form.apply_settings`` with the same value on the same starting loop, and differs from
the row before.

A key is in ``FLAGS`` (its argv, per path where they differ) or a named exception for that path;
a new schema key in neither fails ``test_every_schema_key_is_covered``. Each flag named must be
in the real parser. A key `init` refuses (``INIT_REFUSES``) must exit 2 and write nothing."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import test_apply_every_form_key as ef  # noqa: E402
import test_token_file_settings as tf  # noqa: E402
from diaktoros import cli, config, show  # noqa: E402
from diaktoros.cli import common as cli_common  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402

PUSHES = ef.PUSHES
APP = "events-app[bot]"


def _each(flag, text):
    return [part for value in text.split(", ") for part in (flag, value)]


def _app(role):
    # The fixer's login is not a flag: ``FIRST_FIXER`` puts it first among the --fixer logins.
    login = f"test-{role}[bot]"
    seat = {"reviewer": ["--reviewer-seat", login], "fixer": [],
            "adjudicator": ["--adjudicator-login", login]}[role]
    return lambda t: seat + ["--app", f"{login}=123:{t.value(f'{role}_app_key_file')}"]


# The argv that sets each key to PUSHES[key] (a callable gets the test case). Most keys are their
# own flag with the same value; ``INIT_FLAGS``/``SET_FLAGS`` hold where a path differs.
FLAGS = {key: ["--" + key.replace("_", "-"),
               "on" if value is True else "off" if value is False else value]
         for key, value in PUSHES.items() if not callable(value)}
FLAGS.update({
    "clone": lambda t: ["--clone", str(t.clone2)],
    "turn_budget_s": ["--turn-budget", PUSHES["turn_budget_s"]],
    "operator_agents": _each("--operator-agent", PUSHES["operator_agents"]),
    "required_checks": _each("--required-check", PUSHES["required_checks"]),
    "human_paths": _each("--human-path", PUSHES["human_paths"]),
    "shadow_always_paths": _each("--shadow-always-path", PUSHES["shadow_always_paths"]),
    "shadow_never_paths": _each("--shadow-never-path", PUSHES["shadow_never_paths"]),
    "ci_coverage": ["--ci-covers", PUSHES["ci_coverage"]],
    "prices": ["--price", PUSHES["prices"]],
    "review_only_update": ["--review-only-update", "on", "--acknowledge-branch-push"],
    "ingress": ["--ingress", "app-webhook", "--ingress-app", APP],
    "ingress_app": ["--ingress", "app-webhook", "--ingress-app", APP],
    "adjudicator_login": ["--adjudicator-login", "adj-other"],
    "adjudicator_token_file": lambda t: ["--token", f"{tf.ADJ}={t.pats['adj2']}"],
    "adjudicator_app_id": _app("adjudicator"),
    "adjudicator_app_key_file": _app("adjudicator"),
})
for _gone in ("reviewer_login", "fixer_login", "reviewer_token_file", "fixer_token_file",
              "reviewer_app_id", "reviewer_app_key_file", "fixer_app_id", "fixer_app_key_file",
              "auto_fix_labels", "auto_fix_daily", "fix_daily_turns"):
    FLAGS.pop(_gone, None)
INIT_FLAGS = {
    "reviewer_login": ["--reviewer-seat", "rev-other"],
    "fixer_login": [],
    "reviewer_token_file": lambda t: ["--token", f"{tf.REV}={t.pats['rev2']}"],
    "fixer_token_file": lambda t: ["--token", f"{tf.FIX}={t.pats['fix2']}"],
    **{f"{role}_{field}": _app(role) for role in ("reviewer", "fixer")
       for field in ("app_id", "app_key_file")},
}
# init's fixer seat is the first --fixer login: these keys' runs list that login first.
FIRST_FIXER = {"fixer_login": "fix-other", "fixer_app_id": "test-fixer[bot]",
               "fixer_app_key_file": "test-fixer[bot]"}
SET_FLAGS = {}

_TRIAGE = "on the triage block, set by `hermes dk triage --loop ID` ({}), not by {}"
_SEAT_CREDENTIAL = ("a seat's credential moves with the form and `apply` (or `init --token/--app`); "
                    "`set --token/--app` maps the reader or the adjudicator only")
NOT_BY_INIT = {
    "auto_fix_labels": _TRIAGE.format("--auto-fix-label", "init: a new loop has no triage"),
    "auto_fix_daily": _TRIAGE.format("--auto-fix-daily", "init: a new loop has no triage"),
}
INIT_REFUSES = {
    "fix_daily_turns": (["--fix-daily-turns", "6"], "a new loop has no triage block for the "
                        "cap; init refuses the flag (set it with `dk triage --fix-daily-turns`)"),
}
NOT_BY_SET = {
    "reviewer_profile": "moving a seat to another profile is `apply`'s (cli/seat_flags.py)",
    "fixer_profile": "moving a seat to another profile is `apply`'s (cli/seat_flags.py)",
    "reviewer_login": "a seat login moves with the settings form and `apply`",
    "fixer_login": "a seat login moves with the settings form and `apply`",
    **{f"{role}_{field}": _SEAT_CREDENTIAL for role in ("reviewer", "fixer")
       for field in ("token_file", "app_id", "app_key_file")},
    "auto_fix_labels": _TRIAGE.format("--auto-fix-label", "set"),
    "auto_fix_daily": _TRIAGE.format("--auto-fix-daily", "set"),
    "fix_daily_turns": _TRIAGE.format("--fix-daily-turns", "set"),
}
PATHS = {"init": (INIT_FLAGS, {**NOT_BY_INIT, **INIT_REFUSES}), "set": (SET_FLAGS, NOT_BY_SET)}


class _Ctx:
    def register_cli_command(self, name, summary, setup, **kwargs):
        self.setup = setup


def parser_flags(command):
    ctx = _Ctx()
    cli.register_cli(ctx, settings={})
    parser = argparse.ArgumentParser(prog="hermes dk")
    ctx.setup(parser)
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {o for a in sub.choices[command]._actions for o in a.option_strings}


def rows(loop):
    return {row.name: row.value for row in show._rows(loop) if row.schema}


def _same(value):
    # A scalar compares as text: the form stores ``inflight_ttl_min`` as the string it was given
    # (``normalize`` leaves it to ``LoopState.minutes``), the CLI as an int; both are 12.
    return value if isinstance(value, (list, dict)) else str(value)


def form_settings(t, key):
    """The settings the form pushes for ``key`` (an App identity and app-webhook need pairs)."""
    if key in ("ingress", "ingress_app"):
        return {"ingress": "app-webhook", "ingress_app": APP}
    if "_app_" in key:
        role = key.split("_", 1)[0]
        return {f"{role}_login": f"test-{role}[bot]", f"{role}_app_id": "123",
                f"{role}_app_key_file": t.value(f"{role}_app_key_file")}
    return {key: t.value(key)}


class EveryKeyThroughInitAndSet(unittest.TestCase):
    setUp_fixture = tf.TokenFileSettingsTests.setUp
    setUp_loop = ef.ApplyEveryFormKey.setUp
    pat = tf.TokenFileSettingsTests.pat
    run_cli = tf.TokenFileSettingsTests.run_cli
    init_argv = tf.TokenFileSettingsTests.init_argv
    value = ef.ApplyEveryFormKey.value

    def setUp(self):
        self.setUp_loop()          # the `widgets` loop `set` changes (apply-every-key's)
        self.pats["events"] = self.pat("events")
        self.fixture = config.config_dir() / "widgets.json"
        self.fixture_bytes = self.fixture.read_bytes()

    def argv(self, path, key):
        flags = PATHS[path][0].get(key, FLAGS.get(key))
        return list(flags(self) if callable(flags) else flags)

    def test_every_schema_key_is_covered(self):
        for path, (own, exceptions) in PATHS.items():
            for key in settings_form.SETTINGS_SCHEMA:
                with self.subTest(path=path, key=key):
                    self.assertTrue(key in FLAGS or key in own or key in exceptions,
                                    f"no `{path}` flag, no exception")
                    self.assertFalse(key in own and key in exceptions, "a flag and an exception")
            for key, why in exceptions.items():
                self.assertIn(key, settings_form.SETTINGS_SCHEMA, key)
                self.assertTrue(str(why).strip(), key)

    def test_every_flag_named_is_a_real_flag(self):
        for path, (_, exceptions) in PATHS.items():
            known = parser_flags(path)
            argvs = {k: self.argv(path, k) for k in settings_form.SETTINGS_SCHEMA
                     if k not in exceptions}
            argvs.update({k: argv for k, (argv, _) in INIT_REFUSES.items() if path == "init"})
            for key, argv in argvs.items():
                for word in (w for w in argv if w.startswith("--")):
                    self.assertIn(word, known, f"{key}: `{path}` has no {word}")

    def map_app(self, loop):
        loop["apps"] = {**(loop.get("apps") or {}), APP: {
            "app_id": 9, "key_file": str(self.pats["events"])}}
        cli_common._write_config(loop)

    def assert_written(self, key, before, after, out):
        want = rows(config.normalize(settings_form.apply_settings(before, form_settings(self, key))))
        self.assertNotEqual(want[key], rows(before)[key], f"{key}: the value changes nothing")
        self.assertEqual(_same(rows(after)[key]), _same(want[key]), f"{key}: not written\n{out}")

    def test_set_writes_every_key(self):
        for key in settings_form.SETTINGS_SCHEMA:
            if key in NOT_BY_SET:
                continue
            with self.subTest(key=key), patch.object(cli_common, "_busy_seats", return_value=[]):
                self.fixture.write_bytes(self.fixture_bytes)     # each key from the same loop
                before = config.load_id("widgets")
                if key in ("ingress", "ingress_app"):
                    self.map_app(before)
                rc, out = self.run_cli(["set", "--loop", "widgets", *self.argv("set", key)])
                self.assertEqual(rc, 0, f"{key}: {out}")
                self.assert_written(key, before, config.load_id("widgets"), out)

    def init(self, n, extra, fixer=tf.FIX):
        """``init`` of a fresh loop ``k<n>`` (its own repo and routes) with an adjudicator, a
        shadow, an observer, an App to deliver events and spare logins; its loaded config."""
        argv = ["init", "--repo", f"acme/k{n}", "--read-token", tf.READER,
                "--host", "https://gateway.example", "--clone", str(self.clone),
                "--reviewer-profile", "critic", "--fixer-profile", "coder",
                "--reviewer-seat", tf.REV, "--adjudicator-route", f"k{n}-breach",
                "--adjudicator-profile", "arbiter", "--adjudicator-login", tf.ADJ,
                "--shadow-profile", "critic", "--shadow-every", "1",
                "--observer-profile", "arbiter", "--observer-deliver", "telegram",
                "--app", f"{APP}=9:{self.pats['events']}"]
        for login in (fixer, *(f for f in (tf.FIX, *FIRST_FIXER.values()) if f != fixer)):
            argv += ["--fixer", login]
        for login in (tf.REV, "rev-other", "test-reviewer[bot]"):
            argv += ["--reviewer", login]
        for login, pat in ((tf.READER, "read"), (tf.REV, "rev"), (tf.FIX, "fix"), (tf.ADJ, "adj"),
                           *((name, name) for name in ("rev-other", "fix-other", "adj-other"))):
            argv += ["--token", f"{login}={self.pats[pat]}"]
        rc, out = self.run_cli(argv + list(extra))
        self.assertEqual(rc, 0, out)
        return config.load_id(f"k{n}"), out

    def test_init_writes_every_key(self):
        _, exceptions = PATHS["init"]
        for n, key in enumerate(settings_form.SETTINGS_SCHEMA):
            if key in exceptions:
                continue
            with self.subTest(key=key):
                before, _ = self.init(2 * n, [])
                after, out = self.init(2 * n + 1, self.argv("init", key),
                                       FIRST_FIXER.get(key, tf.FIX))
                self.assert_written(key, before, after, out)

    def test_init_refuses_what_it_cannot_apply(self):
        for key, (argv, _) in INIT_REFUSES.items():
            with self.subTest(key=key):
                rc, out = self.run_cli(self.init_argv("--repo", "acme/refused", *argv))
                self.assertEqual(rc, 2, f"{key}: init accepted a flag it cannot apply\n{out}")
                self.assertFalse((config.config_dir() / "refused.json").exists(), key)


if __name__ == "__main__":
    unittest.main()
